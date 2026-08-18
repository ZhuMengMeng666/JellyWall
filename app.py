# ==========================================
# 1. Python 内置标准库
# ==========================================
import hashlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from queue import Queue

# ==========================================
# 2. 第三方网络与任务调度库
# ==========================================
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ==========================================
# 3. Flask 生态与数据库相关模块
# ==========================================
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, Response, jsonify, stream_with_context
)
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from flask_compress import Compress
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, text, func
from werkzeug.security import generate_password_hash, check_password_hash



# TMDB 搜索结果的短时缓存池，格式为 {query: {timestamp: float, data: list}}
# 使用 OrderedDict 实现 LRU 上限，防止无限增长
TMDB_SEARCH_CACHE = OrderedDict()
# TMDB 详情缓存
TMDB_DETAIL_CACHE = OrderedDict()
# 专门用来缓存剧集的官方总集数
TMDB_TV_EP_COUNT_CACHE = OrderedDict()
# 缓存存活时间
CACHE_TTL = 3600
# 缓存最大条目数，超出后淘汰最久未使用的条目
TMDB_CACHE_MAX_ENTRIES = 200


def _cache_put(cache, key, value):
    """写入缓存并维护 LRU 上限：超限时淘汰最久未使用的条目。"""
    if key in cache:
        cache.pop(key)
    cache[key] = value
    if len(cache) > TMDB_CACHE_MAX_ENTRIES:
        cache.popitem(last=False)
# 全局数据库锁，为了防止多线程把 SQLite 并发写入卡死
db_lock = threading.Lock()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'jellywall_super_secret_key_2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///project.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# 遇到数据库锁时，排队等20秒，防止直接报错崩溃
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'connect_args': {'timeout': 20}
}
# 静态资源浏览器缓存 7 天(文件名不变时直接走本地缓存)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = timedelta(days=7)

Compress(app)


# ==========================================
# 应用版本号(后续迭代在此递增)
# ==========================================
APP_VERSION = "1.0.5"



# ==========================================
# 核心：接管日志引擎 (区分系统 HTTP 与 业务日志)
# ==========================================
log_dir = os.path.join(app.root_path, 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file_path = os.path.join(log_dir, 'jellywall.log')


class SmartFileFormatter(logging.Formatter):
    """
    自定义的智能日志格式化器。
    专门用来区分系统底层的 HTTP 请求和我们自己写的业务日志，顺便把控制台的颜色代码清理掉，保证存入文件的日志干干净净。
    """

    def format(self, record):
        record.asctime = self.formatTime(record, self.datefmt)

        if record.name == 'werkzeug':
            # 如果是底层 HTTP 请求日志，清洗掉原生自带的时间戳，加上咱们的标识前缀
            clean_msg = re.sub(r'\[\d{2}/[A-Za-z]{3}/\d{4} \d{2}:\d{2}:\d{2}\]\s*', '', record.getMessage())
            log_str = f"[System-HTTP] [{record.asctime}] {clean_msg}"
        else:
            # 如果是咱们自己的业务日志，按标准格式加上级别和时间
            log_str = f"[{record.levelname}] [{record.asctime}] {record.getMessage()}"

        # 全局统一剔除控制台可能出现的颜色乱码
        log_str = re.sub(r'\x1b\[[0-9;]*m', '', log_str)
        return log_str


# 文件处理器，输出给前端和文件保存用的
file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
formatter = SmartFileFormatter(datefmt='%Y-%m-%d %H:%M:%S')
file_handler.setFormatter(formatter)

# 控制台处理器，输出在 IDE 终端看
console_handler = logging.StreamHandler(sys.stdout)
console_formatter = logging.Formatter('[%(levelname)s] %(message)s')
console_handler.setFormatter(console_formatter)

# 拦截 Werkzeug 原生日志
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.INFO)
werkzeug_logger.handlers.clear()
werkzeug_logger.addHandler(file_handler)
werkzeug_logger.addHandler(console_handler)

app.logger.handlers.clear()
app.logger.addHandler(file_handler)
app.logger.addHandler(console_handler)
app.logger.setLevel(logging.INFO)

# 初始化业务专属 Logger
logger = logging.getLogger('jellywall')
# 默认 INFO；设置环境变量 JELLYWALL_LOG_LEVEL=DEBUG 可开启 DEBUG 级别的细节日志
logger.setLevel(
    logging.DEBUG if os.environ.get('JELLYWALL_LOG_LEVEL', 'INFO').upper() == 'DEBUG' else logging.INFO
)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


@app.context_processor
def inject_app_version():
    """把版本号注入所有模板,供页面页脚展示。"""
    return {'app_version': APP_VERSION}

# 配置文件的存放路径
CONFIG_DIR = os.path.join(app.root_path, 'config')
os.makedirs(CONFIG_DIR, exist_ok=True)
USERS_FILE = os.path.join(CONFIG_DIR, 'users.json')


def load_users():
    """读取本地的 users.json 文件获取所有的用户列表。"""
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_users(users):
    """把内存里的用户数据覆盖保存到本地的 users.json 里。"""
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=4)


class User(UserMixin):
    """
    用户模型。
    这里没有存进 SQLite 数据库，而是直接用 Python 类来操作 JSON 文件里的用户数据。
    """

    def __init__(self, id, username, password, jellyfin_url=None, jellyfin_api_key=None, jellyfin_user_id=None,
                 proxy_url=None, proxy_port=None, tmdb_api_key=None, web_port=None, sync_enabled=False,
                 sync_cron="0 * * * *"):
        self.id = str(id)
        self.username = username
        self.password = password
        self.jellyfin_url = jellyfin_url
        self.jellyfin_api_key = jellyfin_api_key
        self.jellyfin_user_id = jellyfin_user_id
        self.proxy_url = proxy_url
        self.proxy_port = proxy_port
        self.tmdb_api_key = tmdb_api_key
        self.web_port = web_port
        self.sync_enabled = sync_enabled
        self.sync_cron = sync_cron

    def to_dict(self):
        """把当前用户对象变成字典格式，方便后续转换成 JSON。"""
        return {
            "id": self.id, "username": self.username, "password": self.password,
            "jellyfin_url": self.jellyfin_url, "jellyfin_api_key": self.jellyfin_api_key,
            "jellyfin_user_id": self.jellyfin_user_id, "proxy_url": self.proxy_url,
            "proxy_port": self.proxy_port,
            "tmdb_api_key": self.tmdb_api_key,
            "web_port": self.web_port,
            "sync_enabled": self.sync_enabled,
            "sync_cron": self.sync_cron
        }

    def save(self):
        """保存当前用户的最新信息到本地配置文件。"""
        users = load_users()
        users[self.id] = self.to_dict()
        save_users(users)


class WatchRecord(db.Model):
    """本地观影记录明细表，存用户看过的每一集或者每一部电影。"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), nullable=False)
    item_id = db.Column(db.String(100), nullable=False)
    item_type = db.Column(db.String(50), nullable=False)
    library_name = db.Column(db.String(100), nullable=False)

    title = db.Column(db.String(200), nullable=False)
    series_name = db.Column(db.String(200))
    season_name = db.Column(db.String(100))

    # 专门记录具体是第几集的数字，方便后续排序和统计
    episode_num = db.Column(db.Integer, nullable=True)
    # 记录历史来源，比如是查的 Jellyfin、还是手动 TMDB 添加的
    source = db.Column(db.String(50), nullable=False, default='Jellyfin')
    tmdb_id = db.Column(db.String(50), nullable=True)
    date_played = db.Column(db.DateTime, nullable=False)
    # 软删除标记，用户点击删除时仅仅是打个标记，不做真实物理删除
    is_deleted = db.Column(db.Boolean, default=False)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'item_id', name='_user_item_uc'),
        db.Index('ix_watch_record_user_type', 'user_id', 'is_deleted', 'item_type'),
        db.Index('ix_watch_record_user_series', 'user_id', 'series_name', 'is_deleted'),
        db.Index('ix_watch_record_user_date', 'user_id', 'date_played'),
    )


class WatchPoster(db.Model):
    """海报墙缓存表，用于持久化存储刮削回来的海报路径和电影剧集基本信息。"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), nullable=False)
    target_id = db.Column(db.String(100), nullable=False)
    media_type = db.Column(db.String(50), nullable=False)
    display_title = db.Column(db.String(200), nullable=False)
    series_name = db.Column(db.String(200), nullable=True)
    season_num = db.Column(db.Integer, nullable=True)
    tmdb_id = db.Column(db.String(50), nullable=True)
    local_image_path = db.Column(db.String(255), nullable=False)
    series_image_path = db.Column(db.String(255), nullable=True)
    backdrop_image_path = db.Column(db.String(255), nullable=True)
    background_image_path = db.Column(db.String(255), nullable=True)

    overview = db.Column(db.Text, nullable=True)
    season_overview = db.Column(db.Text, nullable=True)

    last_watched_date = db.Column(db.DateTime, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False)
    __table_args__ = (
        db.UniqueConstraint('user_id', 'target_id', 'display_title', name='_user_poster_uc'),
        db.Index('ix_watch_poster_user_type', 'user_id', 'media_type', 'is_deleted'),
        db.Index('ix_watch_poster_user_series', 'user_id', 'series_name', 'is_deleted'),
    )


class EpisodeDetail(db.Model):
    """单集元数据与剧照缓存表，专门存每一集的具体信息和截图。"""
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.String(100), unique=True, nullable=False)

    series_name = db.Column(db.String(200))
    season_num = db.Column(db.Integer)
    episode_num = db.Column(db.Integer)

    episode_name = db.Column(db.String(200))
    overview = db.Column(db.Text)
    series_tmdb_id = db.Column(db.String(50), nullable=True)
    still_image_path = db.Column(db.String(255))

    __table_args__ = (
        db.Index('ix_episode_detail_series', 'series_tmdb_id', 'season_num'),
    )


def update_episode_detail(item, jf_url, headers, still_dir, series_tmdb_id, session=None):
    """下载并更新某一个特定单集的剧照和剧情简介到本地数据库。"""
    item_id = item["Id"]
    existing_detail = EpisodeDetail.query.filter_by(item_id=item_id).first()
    if existing_detail: return

    series_name = item.get("SeriesName", "未知剧集")
    episode_name = item.get("Name", "未知集名")

    # 遇到没有简介的情况，强制转为空字符串
    overview = item.get("Overview") or ""

    try:
        season_num = int(item.get("ParentIndexNumber")) if item.get("ParentIndexNumber") is not None else None
        episode_num = int(item.get("IndexNumber")) if item.get("IndexNumber") is not None else None
    except ValueError:
        season_num, episode_num = None, None

    still_filename = f"still_{item_id}.jpg"
    still_path = os.path.join(still_dir, still_filename)
    still_relative_path = f"stills/{still_filename}"

    img_url = f"{jf_url}/Items/{item_id}/Images/Primary?maxWidth=600"
    if not download_image(img_url, headers, still_path, session):
        still_relative_path = "images/logo.png"

    new_detail = EpisodeDetail(
        item_id=item_id, series_name=series_name, season_num=season_num, episode_num=episode_num,
        episode_name=episode_name, overview=overview, still_image_path=still_relative_path,
        series_tmdb_id=series_tmdb_id
    )
    db.session.add(new_detail)


# ==========================================
# 后台自动化同步引擎 (APScheduler)
# ==========================================
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def _build_http_session():
    """构建带连接池与自动重试的 requests Session，避免每次请求重建 TCP/TLS 连接。"""
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries, pool_connections=15, pool_maxsize=15)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


def _run_full_sync(user, on_progress=None, use_lock=True, collect_log_names=True):
    """统一的全量同步核心：拉取 Jellyfin 媒体库 → 更新观看记录 / 海报 / 单集详情。

    :param user: User 实例（后台任务中为 load_user 的结果，路由中即 current_user）
    :param on_progress: 可选回调 on_progress(text)，用于 SSE 等实时场景推送进度
    :param use_lock: 是否对每项写入及最终提交使用全局 db_lock
                     （后台/SSE 为 True，手动同步维持原无锁行为为 False）
    :param collect_log_names: 是否额外收集 "剧名 SxxExx" 形式的日志名称
                     （后台/SSE 为 True，手动同步维持原行为为 False）
    :return: {'ok': bool, 'reason': str|None, 'sync_count': int, 'synced_names': set}
    说明：媒体库列表获取失败返回 ok=False（reason='views_failed'）；
    其余网络异常保持原样向上抛出，由各调用方按原有方式处理。
    """
    jf_url = user.jellyfin_url
    headers = {"X-Emby-Token": user.jellyfin_api_key}
    base_user_url = f"{jf_url}/Users/{user.jellyfin_user_id}"

    poster_dir = os.path.join(app.root_path, 'static', 'posters')
    still_dir = os.path.join(app.root_path, 'static', 'stills')
    backdrop_dir = os.path.join(app.root_path, 'static', 'backdrops')
    for d in [poster_dir, still_dir, backdrop_dir]:
        os.makedirs(d, exist_ok=True)

    session = _build_http_session()

    sync_count = 0
    synced_names = set()
    tmdb_search_cache = {}
    processed_ids = set()
    poster_cache = {}

    def do_update(item, view_name, dt_local, master_tmdb_id):
        nonlocal sync_count
        if update_watch_record(user.id, item, item["Type"], view_name, dt_local, master_tmdb_id):
            sync_count += 1

            if collect_log_names:
                if item["Type"] == "Episode":
                    series_name = item.get("SeriesName", item.get("Name", "未知剧集"))
                    try:
                        s_num = int(item.get("ParentIndexNumber", 1))
                        e_num = int(item.get("IndexNumber", 0))
                        log_display_name = f"{series_name} S{s_num:02d}E{e_num:02d}"
                    except (ValueError, TypeError):
                        log_display_name = f"{series_name} (特殊集)"
                else:
                    log_display_name = item.get("Name", "未知电影")

                if log_display_name:
                    synced_names.add(log_display_name)

            update_watch_poster(user.id, user.jellyfin_user_id, item, item["Type"], dt_local,
                                jf_url, headers, poster_dir, backdrop_dir, synced_names,
                                master_tmdb_id, poster_cache, session)

        if item["Type"] == "Episode":
            update_episode_detail(item, jf_url, headers, still_dir, master_tmdb_id, session)

    with db.session.no_autoflush:
        views_resp = session.get(f"{base_user_url}/Views", headers=headers, timeout=10)
        if views_resp.status_code != 200:
            return {'ok': False, 'reason': 'views_failed', 'sync_count': 0, 'synced_names': set()}

        for view in views_resp.json().get("Items", []):
            view_name = view.get('Name', '未知库')
            if on_progress:
                on_progress(f"准备扫描库: {view_name}")

            items_resp = session.get(
                f"{base_user_url}/Items", headers=headers,
                params={"ParentId": view["Id"], "Filters": "IsPlayed", "IncludeItemTypes": "Movie,Episode",
                        "Recursive": "true", "Limit": 2000,
                        "Fields": "UserData,SeriesName,SeriesId,SeasonId,ParentIndexNumber,Overview,ProviderIds,SeriesProviderIds,SeasonName"},
                timeout=15
            )
            if items_resp.status_code != 200:
                continue

            for item in items_resp.json().get("Items", []):
                item_id = item["Id"]
                if item_id in processed_ids:
                    continue
                processed_ids.add(item_id)

                dt_local = parse_jellyfin_date(item.get("UserData", {}).get("LastPlayedDate"))
                if not dt_local:
                    continue

                if on_progress:
                    progress_name = item.get("Name", "未知")
                    if item["Type"] == "Episode":
                        series_name = item.get("SeriesName", "未知剧集")
                        progress_name = f"{series_name} - {progress_name}"
                    on_progress(progress_name)

                master_tmdb_id = get_tmdb_id_smart(user, item, item["Type"], tmdb_search_cache, session)

                if use_lock:
                    with db_lock:
                        do_update(item, view["Name"], dt_local, master_tmdb_id)
                else:
                    do_update(item, view["Name"], dt_local, master_tmdb_id)

        if use_lock:
            with db_lock:
                db.session.commit()
        else:
            db.session.commit()

    return {'ok': True, 'reason': None, 'sync_count': sync_count, 'synced_names': synced_names}


def background_sync_task(user_id):
    """纯后台跑的定时同步任务，负责定期去 Jellyfin 拉取最新的观看进度并存入本地。"""
    with app.app_context():
        user = load_user(user_id)
        if not user or not user.jellyfin_url or not user.jellyfin_api_key:
            return

        logger.info(f"[自动同步] 开始为用户 {user.username} 执行定时同步...")
        try:
            result = _run_full_sync(user, on_progress=None, use_lock=True, collect_log_names=True)
            if not result['ok']:
                logger.warning(f"[自动同步] 用户 {user.username} 同步中止：无法获取媒体库列表")
                return
            sync_count = result['sync_count']
            synced_names = result['synced_names']

            if sync_count > 0:
                names_str = "\n" + "\n".join([f"  - {name}" for name in sorted(synced_names)])
                logger.info(
                    f"[自动同步] 用户 {user.username} 定时同步完成！新增/更新了 {sync_count} 项记录:{names_str}")
            else:
                logger.info(f"[自动同步] 用户 {user.username} 定时同步完成！本地记录已是最新，无新增。")

        except Exception as e:
            logger.error(f"[自动同步] 定时同步失败: {e}")


def archive_yesterday_logs():
    """每天早上 9 点半触发，把昨天的日志抽出来打包成 ZIP 文件压缩备份，原日志保留不变。"""
    with app.app_context():
        try:
            log_dir = os.path.join(app.root_path, 'logs')
            log_file_path = os.path.join(log_dir, 'jellywall.log')

            if not os.path.exists(log_file_path):
                return

            yesterday = datetime.now() - timedelta(days=1)
            date_str = yesterday.strftime('%Y-%m-%d')

            temp_log_name = f"jellywall_{date_str}.log"
            zip_file_path = os.path.join(log_dir, f"jellywall_{date_str}.zip")

            if os.path.exists(zip_file_path):
                return

            yesterday_lines = []
            is_yesterday = False
            date_pattern = re.compile(r'\[(\d{4}-\d{2}-\d{2})\s')

            with open(log_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    match = date_pattern.search(line)
                    if match:
                        log_date = match.group(1)
                        if log_date == date_str:
                            is_yesterday = True
                            yesterday_lines.append(line)
                        else:
                            is_yesterday = False
                    else:
                        # 对于没有时间戳的换行日志（比如报错堆栈），跟随上一行的状态
                        if is_yesterday:
                            yesterday_lines.append(line)

            if not yesterday_lines:
                return

            yesterday_content = "".join(yesterday_lines)

            with zipfile.ZipFile(zip_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.writestr(temp_log_name, yesterday_content)

            logger.info(f"[系统任务] 成功抽取并打包归档昨日日志: {zip_file_path}")

        except Exception as e:
            logger.error(f"[系统任务] 归档日志失败: {str(e)}")


def refresh_scheduler_jobs():
    """读取用户配置，动态清理并重新挂载所有的系统及用户定时任务。"""
    scheduler.remove_all_jobs()
    try:
        scheduler.add_job(
            archive_yesterday_logs,
            trigger=CronTrigger(hour=9, minute=30),
            id="system_log_archive_job",
            replace_existing=True
        )
        logger.info("[调度] 已挂载系统级定时任务: 每天 09:30 自动压缩归档昨日日志")
    except Exception as e:
        logger.error(f"[调度] 挂载系统日志打包任务失败: {e}")

    users = load_users()
    for uid, udata in users.items():
        if udata.get('sync_enabled') and udata.get('sync_cron'):
            try:
                trigger = CronTrigger.from_crontab(udata['sync_cron'])
                scheduler.add_job(
                    background_sync_task,
                    trigger=trigger,
                    args=[uid],
                    id=f"auto_sync_{uid}",
                    replace_existing=True
                )
                logger.info(f"[调度] 已挂载用户 {udata.get('username')} 的定时任务: {udata['sync_cron']}")
            except ValueError:
                logger.warning(f"[调度] 用户 {udata.get('username')} 的 Cron 表达式无效: {udata['sync_cron']}")


@login_manager.user_loader
def load_user(user_id):
    """Flask-Login 框架的回调函数，用来加载用户的实例对象。"""
    users = load_users()
    if str(user_id) in users:
        return User(**users[str(user_id)])
    return None


def _random_backdrop():
    """从本地背景图目录随机选一张 1080p 级背景图（backdrops 为 Jellyfin 1920 宽横图）；
    目录为空或异常时返回 None，由页面回退到纯色光晕样式。"""
    try:
        backdrop_dir = os.path.join(app.root_path, 'static', 'backdrops')
        files = [f for f in os.listdir(backdrop_dir)
                 if f.endswith('.jpg') and not f.startswith('.')]
        if not files:
            return None
        return url_for('static', filename='backdrops/' + random.choice(files))
    except Exception:
        return None


# ================= 路由逻辑 =================

@app.route('/')
def index():
    """网站根路由，没登录去登录，没绑服务器去引导，弄好了就直接进仪表板。"""
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    if not current_user.jellyfin_url or not current_user.jellyfin_api_key:
        return redirect(url_for('onboarding'))
    return redirect(url_for('dashboard'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    """处理用户登录的视图与逻辑校验。"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        users = load_users()
        user_data = next((u for u in users.values() if u.get('username') == username), None)

        if user_data and check_password_hash(user_data['password'], password):
            login_user(User(**user_data))
            logger.info(f"[登录] 用户登录成功: {username}, ip={request.remote_addr}")
            return redirect(url_for('dashboard'))
        else:
            logger.warning(f"[登录] 登录失败，用户名或密码错误: username={username}, ip={request.remote_addr}")
            flash('用户名或密码错误，请重试。')

    return render_template('login.html', bg_url=_random_backdrop(), app_version=APP_VERSION)


@app.route('/register', methods=['GET', 'POST'])
def register():
    """处理新用户注册，管理员可以在系统配置里随时关闭注册入口。"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    sys_config = get_system_config()
    if not sys_config.get('allow_registration', True):
        logger.warning("[注册] 注册入口已被管理员关闭，访问被拒绝")
        flash('管理员已关闭新用户注册功能。')
        return redirect(url_for('login'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        jellyfin_url = request.form.get('jellyfin_url')
        jellyfin_api_key = request.form.get('jellyfin_api_key')

        users = load_users()
        if any(u.get('username') == username for u in users.values()):
            logger.warning(f"[注册] 注册失败，用户名已被占用: {username}")
            flash('该用户名已被注册，请换一个重试。')
            return redirect(url_for('register'))

        new_id = str(uuid.uuid4().hex)
        new_user = User(
            id=new_id,
            username=username,
            password=generate_password_hash(password),
            jellyfin_url=jellyfin_url,
            jellyfin_api_key=jellyfin_api_key
        )

        new_user.save()
        logger.info(f"[注册] 新用户注册成功: {username}")
        flash('注册成功！请登录。')
        return redirect(url_for('login'))

    return render_template('register.html', bg_url=_random_backdrop(), app_version=APP_VERSION)


@app.route('/onboarding', methods=['GET', 'POST'])
@login_required
def onboarding():
    """新手引导页，帮用户打通并绑定 Jellyfin 服务器账号。"""
    if request.method == 'POST':
        host = request.form.get('host').strip()
        port = request.form.get('port').strip()
        username = request.form.get('jf_username').strip()
        password = request.form.get('jf_password')
        is_https = request.form.get('is_https') == 'on'

        host = host.replace('http://', '').replace('https://', '').rstrip('/')
        protocol = "https" if is_https else "http"
        jellyfin_url = f"{protocol}://{host}"
        if port:
            jellyfin_url += f":{port}"

        auth_url = f"{jellyfin_url}/Users/AuthenticateByName"
        device_id = str(uuid.uuid4())
        auth_header = f'MediaBrowser Client="JellyWall", Device="Web", DeviceId="{device_id}", Version="1.0.0"'

        headers = {
            "X-Emby-Authorization": auth_header,
            "Content-Type": "application/json"
        }
        payload = {"Username": username, "Pw": password}

        try:
            resp = requests.post(auth_url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                current_user.jellyfin_url = jellyfin_url
                current_user.jellyfin_api_key = data.get('AccessToken')
                current_user.jellyfin_user_id = data.get('User').get('Id')
                current_user.save()
                logger.info(f"[引导] 用户 {current_user.username} 绑定 Jellyfin 服务器成功: {jellyfin_url}")
                return redirect(url_for('dashboard'))
            elif resp.status_code == 401:
                logger.warning(f"[引导] Jellyfin 绑定失败：用户名或密码错误 (用户: {current_user.username})")
                flash('绑定失败：Jellyfin 用户名或密码错误。')
            else:
                logger.warning(
                    f"[引导] Jellyfin 绑定失败：服务器返回状态码 {resp.status_code} (用户: {current_user.username})")
                flash(f'绑定失败：服务器返回状态码 {resp.status_code}')
        except Exception as e:
            flash(f'无法连接到 Jellyfin，请检查网络或配置。详细: {str(e)}')
            logger.error(f"[引导] Jellyfin 绑定连通性测试失败: {str(e)}")

    return render_template('onboarding.html')


@app.route('/dashboard')
@login_required
def dashboard():
    """仪表板页面，负责查询并聚合用户的各项观影数据，输出各种统计图表所需的数据。"""
    movies_query = WatchRecord.query.filter_by(user_id=current_user.id, item_type='Movie', is_deleted=False)
    movie_total = movies_query.count()
    movie_jf = movies_query.filter_by(source='Jellyfin').count()
    movie_tmdb = movie_total - movie_jf

    eps_query = WatchRecord.query.filter_by(user_id=current_user.id, item_type='Episode', is_deleted=False)
    ep_total = eps_query.count()
    ep_jf = eps_query.filter_by(source='Jellyfin').count()
    ep_tmdb = ep_total - ep_jf

    unique_series = db.session.query(WatchRecord.series_name).filter_by(
        user_id=current_user.id, item_type='Episode', is_deleted=False
    ).distinct().count()

    # 近30天观影统计（电影部数 / 剧集集数）
    thirty_days_ago = datetime.now() - timedelta(days=30)
    recent_30d_rows = db.session.query(
        WatchRecord.item_type, func.count(WatchRecord.id)
    ).filter(
        WatchRecord.user_id == current_user.id,
        WatchRecord.is_deleted == False,
        WatchRecord.date_played >= thirty_days_ago
    ).group_by(WatchRecord.item_type).all()
    movies_30d = 0
    episodes_30d = 0
    for rec_type, cnt in recent_30d_rows:
        if rec_type == 'Movie':
            movies_30d = cnt
        else:
            episodes_30d = cnt

    recent_records_raw = WatchRecord.query.filter_by(user_id=current_user.id, is_deleted=False) \
        .order_by(WatchRecord.date_played.desc()).limit(8).all()

    recent_feed = []
    for rec in recent_records_raw:
        poster_path = "images/logo.png"

        if rec.item_type == 'Movie':
            poster_obj = WatchPoster.query.filter_by(user_id=current_user.id, media_type='Movie',
                                                     display_title=rec.title, is_deleted=False).first()
            if poster_obj: poster_path = poster_obj.local_image_path
        else:
            series_name = getattr(rec, 'series_name', rec.title)
            poster_obj = WatchPoster.query.filter_by(user_id=current_user.id, media_type='Series',
                                                     series_name=series_name, is_deleted=False).first()
            if poster_obj: poster_path = poster_obj.series_image_path or poster_obj.local_image_path

        s_num = 1
        if rec.season_name:
            match = re.search(r'\d+', rec.season_name)
            if match: s_num = int(match.group())
        e_num = rec.episode_num if rec.episode_num is not None else 0
        se_tag = f"S{s_num:02d}E{e_num:02d}" if rec.item_type == 'Episode' else "Movie"

        recent_feed.append({
            'item_type': rec.item_type,
            'title': rec.title,
            'series_name': rec.series_name,
            'se_tag': se_tag,
            'source': rec.source,
            'date_played': rec.date_played,
            'poster_path': poster_path
        })

    one_year_ago = datetime.now() - timedelta(days=365)
    # 优化:由 SQL 按"天 + 类型"分组聚合,替代全量加载后逐条计数
    heat_rows = db.session.query(
        func.date(WatchRecord.date_played).label('d'),
        WatchRecord.item_type,
        func.count(WatchRecord.id).label('cnt')
    ).filter(
        WatchRecord.user_id == current_user.id,
        WatchRecord.is_deleted == False,
        WatchRecord.date_played >= one_year_ago
    ).group_by('d', WatchRecord.item_type).all()

    heatmap_data = {}
    for d, item_type, cnt in heat_rows:
        date_str = str(d)
        if date_str not in heatmap_data:
            heatmap_data[date_str] = {'movies': 0, 'episodes': 0}
        if item_type == 'Movie':
            heatmap_data[date_str]['movies'] = cnt
        else:
            heatmap_data[date_str]['episodes'] = cnt

    return render_template('dashboard.html', title="仪表板",
                           movie_total=movie_total, movie_jf=movie_jf, movie_tmdb=movie_tmdb,
                           ep_total=ep_total, ep_jf=ep_jf, ep_tmdb=ep_tmdb,
                           unique_series=unique_series,
                           movies_30d=movies_30d, episodes_30d=episodes_30d,
                           recent_feed=recent_feed,
                           heatmap_data=heatmap_data)


@app.route('/test_proxy', methods=['POST'])
@login_required
def test_proxy():
    """测试用户填写的代理地址，看网络能不能通。"""
    data = request.json
    proxy_url = data.get('url')
    proxy_port = data.get('port')

    if not proxy_url or not proxy_port:
        return jsonify({"success": False, "message": "请输入完整的代理网址和端口"})

    proxies = {
        "http": f"http://{proxy_url}:{proxy_port}",
        "https": f"http://{proxy_url}:{proxy_port}"
    }

    try:
        test_resp = requests.get("http://www.google.com", proxies=proxies, timeout=5)
        if test_resp.status_code == 200:
            logger.info(f"[网络测试] 代理连通性测试成功: {proxy_url}:{proxy_port}")
            return jsonify({"success": True, "message": "测试代理成功！网络已连通。"})
        else:
            logger.warning(
                f"[网络测试] 代理连通性测试失败：代理服务器返回状态码 {test_resp.status_code} ({proxy_url}:{proxy_port})")
            return jsonify({"success": False, "message": f"测试失败：代理服务器返回状态码 {test_resp.status_code}"})
    except Exception as e:
        logger.warning(f"[网络测试] 代理连通性测试异常：无法连接 {proxy_url}:{proxy_port}: {str(e)}")
        return jsonify({"success": False, "message": f"连接代理失败：{str(e)}"})


@app.route('/test_tmdb', methods=['POST'])
@login_required
def test_tmdb():
    """测试 TMDB 的 API Key 是否有效。"""
    api_key = request.json.get('api_key')
    if not api_key:
        return jsonify({"success": False, "message": "请输入 TMDB API Key"})

    try:
        url = f"https://api.themoviedb.org/3/authentication?api_key={api_key}"
        resp = requests.get(url, proxies=get_user_proxies(current_user), timeout=8)

        if resp.status_code == 200:
            logger.info(f"[网络测试] TMDB API Key 验证通过 (用户: {current_user.username})")
            return jsonify({"success": True, "message": "测试成功！已连通 TMDB。"})
        else:
            logger.warning(
                f"[网络测试] TMDB API Key 验证失败：状态码 {resp.status_code} (用户: {current_user.username})")
            return jsonify({"success": False, "message": f"验证失败：API Key 无效 (状态码 {resp.status_code})"})
    except Exception as e:
        logger.warning(f"[网络测试] TMDB 连通性测试异常: {str(e)}")
        return jsonify({"success": False, "message": f"连接 TMDB 失败，请检查网络或代理设置：{str(e)}"})


@app.route('/config', methods=['GET', 'POST'])
@login_required
def config():
    """配置管理中心，用来统一处理包括代理、密钥、定时同步等各项设置的更新。"""
    if request.method == 'POST':
        form_type = request.form.get('form_type')

        if form_type == 'system_settings':
            allow_reg = 'allow_registration' in request.form
            sys_config = get_system_config()
            sys_config['allow_registration'] = allow_reg
            save_system_config(sys_config)
            logger.info(f"[配置] 用户 {current_user.username} 更新了系统安全设置 (开放注册: {allow_reg})")
            flash("系统安全设置已更新！")
            return redirect(url_for('config'))

        if form_type == 'auto_sync_settings':
            sync_enabled = request.form.get('sync_enabled') == 'on'
            sync_cron = request.form.get('sync_cron').strip()

            current_user.sync_enabled = sync_enabled
            current_user.sync_cron = sync_cron
            current_user.save()
            refresh_scheduler_jobs()

            logger.info(
                f"[配置] 用户 {current_user.username} 更新了自动化同步配置 (enabled={sync_enabled}, cron={sync_cron})")
            flash("自动化同步配置已保存生效！")
            return redirect(url_for('config'))

        if form_type == 'proxy_settings':
            current_user.proxy_url = request.form.get('proxy_url').strip()
            current_user.proxy_port = request.form.get('proxy_port').strip()
            current_user.save()
            logger.info(f"[配置] 用户 {current_user.username} 更新了代理配置")
            flash("代理配置保存成功！")
            return redirect(url_for('config'))

        if form_type == 'tmdb_settings':
            current_user.tmdb_api_key = request.form.get('tmdb_api_key').strip()
            current_user.save()
            logger.info(f"[配置] 用户 {current_user.username} 更新了 TMDB API Key")
            flash("TMDB 密钥保存成功！")
            return redirect(url_for('config'))

        if form_type == 'password_settings':
            old_password = request.form.get('old_password')
            new_password = request.form.get('new_password')
            confirm_password = request.form.get('confirm_password')

            if not check_password_hash(current_user.password, old_password):
                logger.warning(f"[安全] 用户 {current_user.username} 修改密码失败：原密码错误")
                flash("原密码错误，请重试。")
            elif new_password != confirm_password:
                logger.warning(f"[安全] 用户 {current_user.username} 修改密码失败：两次输入的新密码不一致")
                flash("两次输入的新密码不一致。")
            elif len(new_password) < 6:
                logger.warning(f"[安全] 用户 {current_user.username} 修改密码失败：新密码长度不足 6 位")
                flash("新密码长度建议不少于 6 位。")
            else:
                current_user.password = generate_password_hash(new_password)
                logger.info(f"[安全] 用户 {current_user.username} 成功修改了登录密码")
                current_user.save()

                logout_user()
                flash("密码修改成功！请使用新密码重新登录。")
                return redirect(url_for('login'))
            return redirect(url_for('config'))

        if form_type == 'web_settings':
            current_user.web_port = request.form.get('web_port').strip()
            current_user.save()
            logger.info(f"[配置] 用户 {current_user.username} 更新了网页访问端口: {current_user.web_port}")
            flash("网页项目访问端口保存成功！")
            return redirect(url_for('config'))

        protocol = request.form.get('protocol')
        host = request.form.get('host').strip().rstrip('/')
        port = request.form.get('port').strip()
        jf_username = request.form.get('jf_username')
        jf_password = request.form.get('jf_password')

        if host.startswith('http://') or host.startswith('https://'):
            host = host.split('://')[-1]

        base_url = f"{protocol}://{host}:{port}"
        auth_url = f"{base_url}/Users/AuthenticateByName"

        headers = {
            "X-Emby-Authorization": 'MediaBrowser Client="JellyWall", Device="JellyWall Web", DeviceId="JellyWall-V1", Version="1.0.0"',
            "Content-Type": "application/json"
        }
        payload = {
            "Username": jf_username,
            "Pw": jf_password
        }

        try:
            resp = requests.post(auth_url, json=payload, headers=headers, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                access_token = data.get("AccessToken")
                user_id = data.get("User", {}).get("Id")

                current_user.jellyfin_url = base_url
                current_user.jellyfin_api_key = access_token
                current_user.jellyfin_user_id = user_id
                current_user.save()

                logger.info(f"[配置] 用户 {current_user.username} 绑定 Jellyfin 服务器成功: {base_url}")
                flash("Jellyfin 服务器绑定成功！现在可以去拉取数据了。")
            else:
                logger.warning(
                    f"[配置] Jellyfin 绑定失败：账号或密码错误 (状态码: {resp.status_code}, 用户: {current_user.username})")
                flash(f"绑定失败：Jellyfin 账号或密码错误 (错误码: {resp.status_code})")

        except requests.exceptions.RequestException as e:
            logger.warning(f"[配置] Jellyfin 绑定连接失败: {str(e)}")
            flash(f"连接失败：无法访问该地址，请检查 IP、端口或网络是否互通。")

        return redirect(url_for('config'))

    sys_config = get_system_config()
    return render_template('config.html', title="配置管理",
                           allow_registration=sys_config.get('allow_registration', True))


# ==========================================
# TMDB 探索检索中心 (名字层级碰撞深度对齐)
# ==========================================
@app.route('/explore')
@login_required
def explore():
    """渲染探索搜索发现的主页面。"""
    return render_template('explore.html', title="探索发现")


@app.route('/explore_detail/<media_type>/<int:item_id>')
@login_required
def explore_detail(media_type, item_id):
    """TMDB 探索结果详情页，展示电影和剧集的资料库信息，并跟本地 SQLite 里存的观看记录做详细比对碰撞。"""
    api_key = current_user.tmdb_api_key
    if not api_key:
        flash("请先在配置管理中绑定 TMDB API Key！")
        return redirect(url_for('explore'))

    if media_type not in ['movie', 'tv']:
        flash("未知的媒体类型")
        return redirect(url_for('explore'))

    cache_key = f"{media_type}_{item_id}"
    current_time = time.time()
    render_data = None

    if cache_key in TMDB_DETAIL_CACHE:
        cached_item = TMDB_DETAIL_CACHE[cache_key]
        if current_time - cached_item['timestamp'] < CACHE_TTL:
            TMDB_DETAIL_CACHE.move_to_end(cache_key)
            logger.debug(f"[探索详情] 命中 TMDB 详情缓存: {cache_key}")
            render_data = cached_item['data']
        else:
            del TMDB_DETAIL_CACHE[cache_key]

    if not render_data:
        try:
            url = f"https://api.themoviedb.org/3/{media_type}/{item_id}"
            params = {
                "api_key": api_key,
                "language": "zh-CN",
                "append_to_response": "content_ratings,release_dates"
            }
            resp = requests.get(url, params=params, proxies=get_user_proxies(current_user), timeout=10)

            if resp.status_code != 200:
                logger.warning(
                    f"[探索详情] 获取 TMDB 详情失败 (type={media_type}, id={item_id}, status={resp.status_code})")
                flash(f"获取详情失败 (TMDB 状态码: {resp.status_code})")
                return redirect(url_for('explore'))

            data = resp.json()

            title = data.get('title') if media_type == 'movie' else data.get('name')
            overview = data.get('overview') or "这似乎是一部很神秘的影视作品，未抓取到相关的剧情介绍。"
            date_str = data.get('release_date') if media_type == 'movie' else data.get('first_air_date')
            year = date_str[:4] if date_str else "未知"

            poster_path = data.get('poster_path')
            poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else url_for(
                'static',
                filename='images/logo.png')
            backdrop_path = data.get('backdrop_path')
            bg_url = f"https://image.tmdb.org/t/p/w1280{backdrop_path}" if backdrop_path else poster_url
            # 小尺寸背景图（1x 设备用），配合模板 image-set() 按 DPR 选择，减少低分辨率屏下载量
            bg_url_1x = f"https://image.tmdb.org/t/p/w780{backdrop_path}" if backdrop_path else None
            display_type = 'series' if media_type == 'tv' else 'movie'

            genres_list = [g.get('name') for g in data.get('genres', []) if g.get('name')]
            genres_str = ", ".join(genres_list) if genres_list else "未知类型"

            rating = "NR"
            if media_type == 'tv':
                c_ratings = data.get('content_ratings', {}).get('results', [])
                for cr in c_ratings:
                    if cr.get('iso_3166_1') == 'US':
                        rating = cr.get('rating')
                        break
            else:
                r_dates = data.get('release_dates', {}).get('results', [])
                for rd in r_dates:
                    if rd.get('iso_3166_1') == 'US':
                        cert_info = rd.get('release_dates', [{}])[0]
                        rating = cert_info.get('certification') or "NR"
                        break
            if not rating:
                rating = "NR"

            seasons_data = {}
            season_poster_map = {}
            season_overview_map = {}

            if media_type == 'tv':
                raw_seasons = data.get('seasons', [])
                logo_url = url_for('static', filename='images/logo.png')
                user_proxies = get_user_proxies(current_user)

                def fetch_season(s):
                    s_num = s.get('season_number')
                    if s_num is None:
                        return None

                    s_url = f"https://api.themoviedb.org/3/tv/{item_id}/season/{s_num}"
                    s_resp = requests.get(s_url, params={"api_key": api_key, "language": "zh-CN"},
                                          proxies=user_proxies, timeout=5)

                    if s_resp.status_code != 200:
                        return None

                    s_data = s_resp.json()
                    episodes = s_data.get('episodes', [])

                    formatted_episodes = []
                    for ep in episodes:
                        still_path = ep.get('still_path')
                        full_still_url = f"https://image.tmdb.org/t/p/w300{still_path}" if still_path else logo_url

                        formatted_episodes.append({
                            'episode_num': ep.get('episode_number'),
                            'title': ep.get('name'),
                            'overview': ep.get('overview'),
                            'still_path': full_still_url,
                            'air_date': ep.get('air_date') or '未知首播时间'
                        })

                    return {
                        's_num': s_num,
                        'episodes': formatted_episodes,
                        'poster_path': s.get('poster_path'),
                        'overview': s.get('overview') or s_data.get('overview') or ""
                    }

                if raw_seasons:
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        for season_result in pool.map(fetch_season, raw_seasons):
                            if season_result is None:
                                continue
                            s_num = season_result['s_num']
                            seasons_data[s_num] = season_result['episodes']
                            s_poster = season_result['poster_path']
                            season_poster_map[
                                s_num] = f"https://image.tmdb.org/t/p/w300{s_poster}" if s_poster else poster_url
                            season_overview_map[s_num] = season_result['overview']

            render_data = {
                'title': title,
                'media_type': display_type,
                'year': year,
                'genres': genres_str,
                'rating': rating,
                'overview': overview,
                'poster_url': poster_url,
                'bg_url': bg_url,
                'bg_url_1x': bg_url_1x,
                'seasons': seasons_data,
                'season_poster_map': season_poster_map,
                'season_overview_map': season_overview_map
            }

            _cache_put(TMDB_DETAIL_CACHE, cache_key, {
                'timestamp': current_time,
                'data': render_data
            })
        except Exception as e:
            logger.error(f"[探索详情] 获取 TMDB 详情异常 (type={media_type}, id={item_id}): {str(e)}")
            flash(f"网络请求失败: {str(e)}")
            return redirect(url_for('explore'))

    is_movie_watched = False
    is_series_watched = False

    watched_episodes_dict = {}
    season_watch_status = {}
    has_watched_any = False

    import re
    title_to_check = render_data['title']

    if render_data['media_type'] == 'movie':
        movie_exist = db.session.query(WatchPoster.id).filter_by(
            user_id=current_user.id, media_type='Movie', display_title=title_to_check, is_deleted=False
        ).first()
        if movie_exist:
            is_movie_watched = True
    else:
        ep_records = WatchRecord.query.filter_by(
            user_id=current_user.id, item_type='Episode', series_name=title_to_check, is_deleted=False
        ).all()

        for ep in ep_records:
            s_num = 1
            if ep.season_name:
                match = re.search(r'\d+', ep.season_name)
                if match: s_num = int(match.group())
            e_num = ep.episode_num

            if e_num is not None:
                date_str = ep.date_played.strftime('%Y-%m-%d %H:%M') if ep.date_played else "未知时间"
                watched_episodes_dict[f"{s_num}_{e_num}"] = date_str

        seasons_dict = render_data.get('seasons', {})
        for s_num, eps_list in seasons_dict.items():
            total_eps = len(eps_list)
            if total_eps == 0:
                continue

            watched_count = sum(
                1 for ep_info in eps_list if f"{s_num}_{ep_info.get('episode_num')}" in watched_episodes_dict)

            if watched_count > 0:
                has_watched_any = True
                if watched_count >= total_eps:
                    season_watch_status[s_num] = "full"
                else:
                    season_watch_status[s_num] = "partial"

        if has_watched_any:
            is_series_watched = True
            valid_s_count = 0
            for s_num, eps_list in seasons_dict.items():
                if s_num > 0 and len(eps_list) > 0:
                    valid_s_count += 1
                    if season_watch_status.get(s_num) != "full":
                        is_series_watched = False
                        break

            if valid_s_count == 0:
                for s_num, eps_list in seasons_dict.items():
                    if len(eps_list) > 0 and season_watch_status.get(s_num) != "full":
                        is_series_watched = False
                        break

    return render_template('explore_detail.html',
                           is_movie_watched=is_movie_watched,
                           is_series_watched=is_series_watched,
                           watched_episodes_dict=watched_episodes_dict,
                           season_watch_status=season_watch_status,
                           has_watched_any=has_watched_any,
                           **render_data)


def download_tmdb_image(url, folder, filename, user_proxies=None):
    """辅助下载 TMDB 影视图片到本地存储区，防止一直去公网拉图片。如果本地已经有这个图了就直接跳过。"""
    try:
        os.makedirs(folder, exist_ok=True)
        filepath = os.path.join(folder, filename)
        if os.path.exists(filepath):
            return filename

        resp = requests.get(url, proxies=user_proxies, timeout=15)
        if resp.status_code == 200:
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            return filename
    except Exception as e:
        logger.warning(f"[TMDB图片] 图片本地化失败: {e}")
    return None


@app.route('/api/explore/mark_watched', methods=['POST'])
@login_required
def api_mark_watched():
    """在探索页面点击了添加观看记录后，手动把 TMDB 数据反向同步进本地数据库里，还会一起刮削集数信息。"""
    api_key = current_user.tmdb_api_key
    if not api_key:
        logger.warning(f"[反向同步] 未绑定 TMDB API Key，拒绝反向同步请求 (用户: {current_user.username})")
        return jsonify({"success": False, "message": "未绑定 TMDB API Key"})

    req_data = request.json or {}
    media_type = req_data.get('media_type')
    if media_type == 'tv':
        media_type = 'series'

    item_id = req_data.get('item_id')
    scope = req_data.get('scope')
    target_season = req_data.get('season_num')
    target_episode = req_data.get('episode_num')

    if not media_type or not item_id or not scope:
        return jsonify({"success": False, "message": "缺少必要请求参数"})

    proxies = get_user_proxies(current_user)
    now = datetime.now()

    try:
        tmdb_type = 'movie' if media_type == 'movie' else 'tv'
        url = f"https://api.themoviedb.org/3/{tmdb_type}/{item_id}"
        resp = requests.get(url, params={"api_key": api_key, "language": "zh-CN"}, proxies=proxies, timeout=10)
        if resp.status_code != 200:
            return jsonify({"success": False, "message": "无法从 TMDB 拉取该影视的基础元数据"})

        data = resp.json()
        title = data.get('title') if tmdb_type == 'movie' else data.get('name')
        poster_path = data.get('poster_path')
        overview = data.get('overview') or ""

        if not title:
            return jsonify({"success": False, "message": "影视数据解析失败，未获取到有效名称"})

        local_poster_name = None
        if poster_path:
            remote_poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}"
            local_poster_name = download_tmdb_image(
                remote_poster_url,
                os.path.join(app.root_path, 'static', 'posters'),
                f"tmdb_{item_id}.jpg",
                proxies
            )
        relative_main_poster_path = f"posters/{local_poster_name}" if local_poster_name else "images/logo.png"

        tmdb_seasons_map = {}
        if media_type == 'series':
            for s in data.get('seasons', []):
                s_num = s.get('season_number')
                if s_num is not None:
                    tmdb_seasons_map[s_num] = {
                        'poster_path': s.get('poster_path'),
                        'overview': s.get('overview') or ""
                    }

        def ensure_season_poster(s_num):
            """内部闭包函数：保证某季的海报信息被创建或者更新。"""
            target_id = f"{item_id}_S{s_num}"
            poster_record = WatchPoster.query.filter_by(
                user_id=current_user.id, target_id=target_id
            ).first()

            if poster_record:
                poster_record.is_deleted = False
                poster_record.last_watched_date = now
            else:
                s_info = tmdb_seasons_map.get(s_num, {})
                s_poster_path = s_info.get('poster_path')
                s_overview = s_info.get('overview') or ""

                local_s_poster_name = None
                if s_poster_path:
                    local_s_poster_name = download_tmdb_image(
                        f"https://image.tmdb.org/t/p/w500{s_poster_path}",
                        os.path.join(app.root_path, 'static', 'posters'),
                        f"tmdb_{item_id}_S{s_num}.jpg",
                        proxies
                    )
                relative_s_poster = f"posters/{local_s_poster_name}" if local_s_poster_name else relative_main_poster_path

                poster_record = WatchPoster(
                    user_id=current_user.id,
                    target_id=target_id,
                    media_type='Series',
                    display_title=f"{title} (第 {s_num} 季)" if s_num != 0 else f"{title} (特别篇)",
                    series_name=title,
                    season_num=s_num,
                    local_image_path=relative_s_poster,
                    series_image_path=relative_main_poster_path,
                    overview=overview,
                    season_overview=s_overview,
                    last_watched_date=now,
                    is_deleted=False,
                    tmdb_id=str(item_id)
                )
                db.session.add(poster_record)

        def ensure_episode_detail(s_num, e_num, ep_name, ep_overview, ep_still_path):
            """内部闭包函数：创建或者更新单集的剧情跟剧照。"""
            ep_item_id = f"{item_id}_{s_num}_{e_num}"
            existing_detail = EpisodeDetail.query.filter_by(item_id=ep_item_id).first()
            if not existing_detail:
                local_still_name = None
                if ep_still_path:
                    os.makedirs(os.path.join(app.root_path, 'static', 'stills'), exist_ok=True)
                    local_still_name = download_tmdb_image(
                        f"https://image.tmdb.org/t/p/w300{ep_still_path}",
                        os.path.join(app.root_path, 'static', 'stills'),
                        f"still_tmdb_{ep_item_id}.jpg",
                        proxies
                    )
                relative_still_path = f"stills/still_tmdb_{ep_item_id}.jpg" if local_still_name else "images/logo.png"

                new_detail = EpisodeDetail(
                    item_id=ep_item_id,
                    series_name=title,
                    season_num=s_num,
                    episode_num=e_num,
                    episode_name=ep_name or f"第 {e_num} 集",
                    overview=ep_overview or "",
                    series_tmdb_id=str(item_id),
                    still_image_path=relative_still_path
                )
                db.session.add(new_detail)

        if media_type == 'movie':
            poster_record = WatchPoster.query.filter_by(
                user_id=current_user.id, target_id=str(item_id)
            ).first()

            if poster_record:
                poster_record.is_deleted = False
                poster_record.last_watched_date = now
            else:
                poster_record = WatchPoster(
                    user_id=current_user.id,
                    target_id=str(item_id),
                    media_type='Movie',
                    display_title=title,
                    local_image_path=relative_main_poster_path,
                    overview=overview,
                    last_watched_date=now,
                    is_deleted=False,
                    tmdb_id=str(item_id)
                )
                db.session.add(poster_record)

        if scope == 'movie':
            exist = WatchRecord.query.filter_by(
                user_id=current_user.id, item_id=str(item_id)
            ).first()
            if exist:
                exist.is_deleted = False
                exist.date_played = now
            else:
                rec = WatchRecord(
                    user_id=current_user.id,
                    item_id=str(item_id),
                    item_type='Movie',
                    library_name='TMDB添加',
                    title=title,
                    date_played=now,
                    source='tmdb',
                    is_deleted=False,
                    tmdb_id=str(item_id)
                )
                db.session.add(rec)

        elif scope == 'episode':
            ensure_season_poster(target_season)
            ep_url = f"https://api.themoviedb.org/3/tv/{item_id}/season/{target_season}/episode/{target_episode}"
            ep_resp = requests.get(ep_url, params={"api_key": api_key, "language": "zh-CN"}, proxies=proxies, timeout=8)
            ep_title = f"第 {target_episode} 集"
            ep_overview = ""
            ep_still_path = None
            if ep_resp.status_code == 200:
                ep_data = ep_resp.json()
                ep_title = ep_data.get('name') or ep_title
                ep_overview = ep_data.get('overview') or ""
                ep_still_path = ep_data.get('still_path')

            season_str = f"第 {target_season} 季" if target_season != 0 else "特别篇"
            ep_item_id = f"{item_id}_{target_season}_{target_episode}"

            exist = WatchRecord.query.filter_by(
                user_id=current_user.id, item_id=ep_item_id
            ).first()
            if exist:
                exist.is_deleted = False
                exist.date_played = now
            else:
                rec = WatchRecord(
                    user_id=current_user.id,
                    item_id=ep_item_id,
                    item_type='Episode',
                    library_name='TMDB添加',
                    title=f"第 {target_episode} 集 - {ep_title}",
                    series_name=title,
                    season_name=season_str,
                    episode_num=target_episode,
                    date_played=now,
                    source='tmdb',
                    is_deleted=False,
                    tmdb_id=str(item_id)
                )
                db.session.add(rec)
            ensure_episode_detail(target_season, target_episode, ep_title, ep_overview, ep_still_path)

        elif scope == 'episode_batch':
            episodes_list = req_data.get('episodes_list', [])
            batch_by_season = {}
            for ep_info in episodes_list:
                s_num = ep_info.get('season')
                e_num = ep_info.get('episode')
                if s_num is not None and e_num is not None:
                    batch_by_season.setdefault(s_num, []).append(e_num)

            for s_num, e_nums in batch_by_season.items():
                ensure_season_poster(s_num)
                s_url = f"https://api.themoviedb.org/3/tv/{item_id}/season/{s_num}"
                s_resp = requests.get(s_url, params={"api_key": api_key, "language": "zh-CN"}, proxies=proxies,
                                      timeout=8)
                ep_meta_map = {}
                if s_resp.status_code == 200:
                    for ep in s_resp.json().get('episodes', []):
                        ep_meta_map[ep.get('episode_number')] = ep

                for e_num in e_nums:
                    ep_data = ep_meta_map.get(e_num, {})
                    ep_title = ep_data.get('name') or f"第 {e_num} 集"
                    ep_overview = ep_data.get('overview') or ""
                    ep_still_path = ep_data.get('still_path')
                    season_str = f"第 {s_num} 季" if s_num != 0 else "特别篇"
                    ep_item_id = f"{item_id}_{s_num}_{e_num}"

                    exist = WatchRecord.query.filter_by(
                        user_id=current_user.id, item_id=ep_item_id
                    ).first()

                    if exist:
                        exist.is_deleted = False
                        exist.date_played = now
                    else:
                        rec = WatchRecord(
                            user_id=current_user.id,
                            item_id=ep_item_id,
                            item_type='Episode',
                            library_name='TMDB添加',
                            title=f"第 {e_num} 集 - {ep_title}",
                            series_name=title,
                            season_name=season_str,
                            episode_num=e_num,
                            date_played=now,
                            source='tmdb',
                            is_deleted=False,
                            tmdb_id=str(item_id)
                        )
                        db.session.add(rec)
                    ensure_episode_detail(s_num, e_num, ep_title, ep_overview, ep_still_path)

        elif scope in ['season', 'series']:
            seasons_to_add = [target_season] if scope == 'season' else []
            if scope == 'series':
                raw_seasons = data.get('seasons', [])
                seasons_to_add = [s.get('season_number') for s in raw_seasons if s.get('season_number') is not None]

            for s_num in seasons_to_add:
                ensure_season_poster(s_num)
                s_url = f"https://api.themoviedb.org/3/tv/{item_id}/season/{s_num}"
                s_resp = requests.get(s_url, params={"api_key": api_key, "language": "zh-CN"}, proxies=proxies,
                                      timeout=8)
                if s_resp.status_code == 200:
                    eps = s_resp.json().get('episodes', [])
                    for ep in eps:
                        e_num = ep.get('episode_number')
                        if e_num is None: continue

                        season_str = f"第 {s_num} 季" if s_num != 0 else "特别篇"
                        ep_item_id = f"{item_id}_{s_num}_{e_num}"

                        exist = WatchRecord.query.filter_by(
                            user_id=current_user.id, item_id=ep_item_id
                        ).first()

                        if exist:
                            exist.is_deleted = False
                            exist.date_played = now
                        else:
                            rec = WatchRecord(
                                user_id=current_user.id,
                                item_id=ep_item_id,
                                item_type='Episode',
                                library_name='TMDB添加',
                                title=f"第 {e_num} 集 - {ep.get('name', '未知集名')}",
                                series_name=title,
                                season_name=season_str,
                                episode_num=e_num,
                                date_played=now,
                                source='tmdb',
                                is_deleted=False,
                                tmdb_id=str(item_id)
                            )
                            db.session.add(rec)

                        ensure_episode_detail(s_num, e_num, ep.get('name'), ep.get('overview'), ep.get('still_path'))

        db.session.commit()
        log_msg = f"用户 {current_user.username} 通过探索页手动补录了"
        if scope == 'movie':
            log_msg += f"电影《{title}》"
        elif scope == 'series':
            log_msg += f"整部剧集《{title}》"
        elif scope == 'season':
            season_str = f"第 {target_season} 季" if target_season != 0 else "特别篇"
            log_msg += f"剧集《{title}》{season_str}"
        elif scope == 'episode':
            season_str = f"第 {target_season} 季" if target_season != 0 else "特别篇"
            log_msg += f"剧集《{title}》{season_str} 第 {target_episode} 集"
        elif scope == 'episode_batch':
            ep_count = len(req_data.get('episodes_list', []))
            log_msg += f"剧集《{title}》中的 {ep_count} 个单集"

        logger.info(f"[反向同步] {log_msg}的观看足迹。")
        return jsonify({"success": True, "message": "已成功同步并下载本地海报与元数据！"})

    except Exception as e:
        db.session.rollback()
        logger.error(f"[反向同步] 反向同步操作失败 (用户: {current_user.username}): {str(e)}")
        return jsonify({"success": False, "message": f"反向同步操作失败: {str(e)}"})


@app.route('/api/search_tmdb')
@login_required
def api_search_tmdb():
    """
    TMDB 的搜索接口，直接带内存缓存防限速，并且请求回来之后会和本地数据库比对，看这片子到底看没看过。
    """
    query = request.args.get('q')
    if not query:
        return jsonify({"success": False, "message": "搜索词不能为空"})

    api_key = current_user.tmdb_api_key
    if not api_key:
        return jsonify({"success": False, "message": "请先在配置管理中绑定 TMDB API Key"})

    current_time = time.time()
    raw_results = None

    if query in TMDB_SEARCH_CACHE:
        cached_item = TMDB_SEARCH_CACHE[query]
        if current_time - cached_item['timestamp'] < CACHE_TTL:
            TMDB_SEARCH_CACHE.move_to_end(query)
            raw_results = cached_item['data']
            logger.debug(f"[搜索缓存] 命中 TMDB 搜索缓存: {query}")
        else:
            del TMDB_SEARCH_CACHE[query]

    if raw_results is None:
        try:
            url = "https://api.themoviedb.org/3/search/multi"
            params = {
                "api_key": api_key,
                "query": query,
                "language": "zh-CN",
                "page": 1,
                "include_adult": "false"
            }

            resp = requests.get(url, params=params, proxies=get_user_proxies(current_user), timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                raw_results = data.get('results', [])

                _cache_put(TMDB_SEARCH_CACHE, query, {
                    'timestamp': current_time,
                    'data': raw_results
                })
                logger.debug(f"[搜索缓存] 未命中缓存，请求 TMDB 并写入: {query}")
            else:
                return jsonify({"success": False, "message": f"TMDB 返回异常 (状态码: {resp.status_code})"})

        except Exception as e:
            return jsonify({"success": False, "message": f"网络请求失败，请检查代理配置: {str(e)}"})

    if not raw_results:
        return jsonify({"success": True, "results": []})

    try:
        local_movies = db.session.query(WatchPoster.display_title) \
            .filter(WatchPoster.user_id == current_user.id, WatchPoster.media_type == 'Movie',
                    WatchPoster.is_deleted == False).all()
        watched_movies_set = {r[0] for r in local_movies if r[0]}

        local_series = db.session.query(WatchPoster.series_name) \
            .filter(WatchPoster.user_id == current_user.id, WatchPoster.media_type == 'Series',
                    WatchPoster.is_deleted == False).all()
        watched_series_set = {r[0] for r in local_series if r[0]}

        # 优化:一次性批量拉取所有"已看过"剧集的本地记录,替代逐个结果查询
        watched_tv_names = [item.get('name') for item in raw_results
                            if item.get('media_type') == 'tv' and item.get('name') in watched_series_set]
        ep_records_by_series = {}
        if watched_tv_names:
            tv_ep_records = WatchRecord.query.filter(
                WatchRecord.user_id == current_user.id,
                WatchRecord.item_type == 'Episode',
                WatchRecord.series_name.in_(watched_tv_names),
                WatchRecord.is_deleted == False
            ).all()
            for ep in tv_ep_records:
                ep_records_by_series.setdefault(ep.series_name, []).append(ep)

        results = []
        import re
        for item in raw_results:
            media_type = item.get('media_type')
            if media_type in ['movie', 'tv']:
                tmdb_name = item.get('title') if media_type == 'movie' else item.get('name')
                if not tmdb_name:
                    continue

                date = item.get('release_date') if media_type == 'movie' else item.get('first_air_date')
                poster_path = item.get('poster_path')
                item_id = item.get('id')

                watch_status = 'none'

                if media_type == 'movie':
                    if tmdb_name in watched_movies_set:
                        watch_status = 'watched'
                else:
                    if tmdb_name in watched_series_set:
                        total_eps = 0
                        if item_id in TMDB_TV_EP_COUNT_CACHE:
                            TMDB_TV_EP_COUNT_CACHE.move_to_end(item_id)
                            total_eps = TMDB_TV_EP_COUNT_CACHE[item_id]
                        else:
                            try:
                                tv_resp = requests.get(
                                    f"https://api.themoviedb.org/3/tv/{item_id}",
                                    params={"api_key": api_key},
                                    proxies=get_user_proxies(current_user), timeout=3)
                                if tv_resp.status_code == 200:
                                    total_eps = tv_resp.json().get('number_of_episodes', 0)
                                    _cache_put(TMDB_TV_EP_COUNT_CACHE, item_id, total_eps)
                            except:
                                pass

                        ep_records = ep_records_by_series.get(tmdb_name, [])
                        watched_normal_eps = set()
                        for ep in ep_records:
                            s_num = 1
                            if ep.season_name:
                                match = re.search(r'\d+', ep.season_name)
                                if match: s_num = int(match.group())
                            if s_num > 0 and ep.episode_num is not None:
                                watched_normal_eps.add(f"{s_num}_{ep.episode_num}")

                        local_count = len(watched_normal_eps)

                        if total_eps > 0 and local_count >= total_eps:
                            watch_status = 'watched'
                        elif local_count > 0:
                            watch_status = 'watching'
                        else:
                            watch_status = 'none'
                results.append({
                    'id': item_id,
                    'media_type': media_type,
                    'title': tmdb_name,
                    'date': date[:4] if date else "未知年份",
                    'poster_url': f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
                    'watch_status': watch_status
                })

        return jsonify({"success": True, "results": results})

    except Exception as e:
        return jsonify({"success": False, "message": f"本地数据碰撞异常: {str(e)}"})


@app.route('/watched')
@login_required
def watched():
    """读取本地缓存：从观看记录提取最真实的初看时间，从海报表匹配最高清图片，完美去重渲染海报墙。"""

    # ==========================================
    # 1. 抓取海报表，构建基于片名和 TMDB_ID 的图片映射库
    # ==========================================
    posters = db.session.query(
        WatchPoster.media_type, WatchPoster.display_title, WatchPoster.series_name,
        WatchPoster.tmdb_id, WatchPoster.local_image_path, WatchPoster.series_image_path
    ).filter_by(user_id=current_user.id, is_deleted=False).all()
    movie_poster_map = {}
    movie_tmdb_map = {}
    series_poster_map = {}
    series_tmdb_map = {}

    for p in posters:
        if p.media_type == "Movie":
            movie_poster_map[p.display_title] = p.local_image_path
            if p.tmdb_id:
                movie_tmdb_map[str(p.tmdb_id)] = p.local_image_path
        else:
            path = p.series_image_path or p.local_image_path
            series_poster_map[p.series_name] = path
            if p.tmdb_id:
                series_tmdb_map[str(p.tmdb_id)] = path

    def is_valid_tmdb(tid):
        return bool(tid and str(tid).lower() not in ['none', 'null', ''])

    # ==========================================
    # 2. 从真实的观看记录表 (WatchRecord) 中提取数据
    # 注意这里使用的是 asc()，即从最老的远古记录开始遍历
    # ==========================================
    raw_records = db.session.query(
        WatchRecord.id, WatchRecord.item_type, WatchRecord.title,
        WatchRecord.series_name, WatchRecord.tmdb_id, WatchRecord.date_played
    ).filter_by(user_id=current_user.id, is_deleted=False).order_by(WatchRecord.date_played.asc()).all()

    aggregated_dict = {}

    # ==========================================
    # 3. 核心去重与时间捕获引擎
    # ==========================================
    for record in raw_records:
        item_type = record.item_type
        tmdb_id = record.tmdb_id
        valid_tmdb = is_valid_tmdb(tmdb_id)

        # 构建统一身份键，没有 tmdb_id 就强制使用中文片名兜底，杜绝重复
        if item_type == "Movie":
            key = f"movie_tmdb_{tmdb_id}" if valid_tmdb else f"movie_title_{record.title}"
            name = record.title
            type_icon = "M"
            # 智能匹配图片
            if valid_tmdb and str(tmdb_id) in movie_tmdb_map:
                img_file = movie_tmdb_map[str(tmdb_id)]
            else:
                img_file = movie_poster_map.get(name)
        else:
            series_name = getattr(record, 'series_name', record.title)
            key = f"series_tmdb_{tmdb_id}" if valid_tmdb else f"series_title_{series_name}"
            name = series_name
            type_icon = "S"
            # 智能匹配图片
            if valid_tmdb and str(tmdb_id) in series_tmdb_map:
                img_file = series_tmdb_map[str(tmdb_id)]
            else:
                img_file = series_poster_map.get(name)

        # ✨ 最关键的一步：因为列表已经是 asc 升序
        # 只要这个 key 不在字典里，说明我们遇到了这部剧的【绝对第一次】观看记录
        # 我们把它锁进字典里。后续再遇到这部剧的第二季、第三季记录，直接忽略，从而保住了最古老的时间
        if key not in aggregated_dict:
            aggregated_dict[key] = {
                "id": record.id,
                "name": name,
                "type_icon": type_icon,
                "local_img_url": url_for('static', filename=img_file) if img_file else url_for('static',
                                                                                               filename='images/logo.png'),
                "date_actual": record.date_played
            }

    # 4. 把字典转回列表，再执行一次排序（确保渲染时的严格顺序）
    final_posters = list(aggregated_dict.values())
    final_posters.sort(key=lambda x: x["date_actual"])

    movies_data = []
    for item in final_posters:
        movies_data.append({
            "id": item["id"],
            "name": item["name"],
            "type_icon": item["type_icon"],
            "local_img_url": item["local_img_url"],
            "date_formatted": item["date_actual"].strftime("%Y-%m-%d %H:%M")
        })

    return render_template('watched.html', title="海报墙", movies=movies_data)


def _render_changelog_markdown(text):
    """将 CHANGELOG.md 的子集转换为带语义 class 的 HTML：版本卡片、分类图标、简约符号。"""
    # 分类 emoji → lucide 图标映射（页面展示用；CHANGELOG.md 原文保留 emoji 供 GitHub 渲染）
    category_icons = {
        '\U0001f41b Bug\u4fee\u590d': 'bug',
        '\U0001f680 \u4f18\u5316': 'zap',
        '\U0001f4da \u6587\u6863\u66f4\u65b0': 'book-open',
    }
    lines = text.strip().splitlines()
    html = []
    in_list = False
    in_card = False
    for raw in lines:
        line = raw.strip()
        if not line:
            if in_list:
                html.append('</ul>')
                in_list = False
            continue
        if line.startswith('#'):
            if in_list:
                html.append('</ul>')
                in_list = False
            level = min(len(line) - len(line.lstrip('#')), 3)
            content = line.lstrip('#').strip()
            if level == 1:
                # 跳过文件主标题（页面已有"更新日志"标题）
                continue
            elif level == 2:
                if in_card:
                    html.append('</div>')
                html.append('<div class="cl-card">')
                html.append(f'<h2 class="cl-version">{content}</h2>')
                in_card = True
            else:
                html.append(f'<h{level}>{content}</h{level}>')
        elif line.startswith('- '):
            if not in_list:
                html.append('<ul class="cl-list">')
                in_list = True
            html.append(f'<li>{line[2:]}</li>')
        else:
            if in_list:
                html.append('</ul>')
                in_list = False
            if line == '**\u6700\u65b0**':
                html.append('<p class="cl-latest">\u6700\u65b0</p>')
            elif re.match(r'^\d{4}/\d{2}/\d{2}', line):
                html.append(f'<p class="cl-date">{line}</p>')
            elif line.startswith('**') and line.endswith('**') and len(line) > 4:
                icon = category_icons.get(line[2:-2], '')
                if icon:
                    html.append(
                        f'<h3 class="cl-category"><i data-lucide="{icon}" class="cl-cat-icon"></i>{line[2:-2]}</h3>')
                else:
                    html.append(f'<h3 class="cl-category">{line}</h3>')
            else:
                html.append(f'<p class="cl-plain">{line}</p>')
    if in_list:
        html.append('</ul>')
    if in_card:
        html.append('</div>')
    result = '\n'.join(html)
    result = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', result)
    result = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank" rel="noopener">\1</a>', result)
    # 版本标题：去掉 emoji 星标，改用 lucide star 图标
    result = result.replace('\u2b50 ', '')
    result = re.sub(r'<h2 class="cl-version">(<a[^>]*>)',
                    r'<h2 class="cl-version"><i data-lucide="star" class="cl-ver-icon"></i>\1', result)
    return result


def _load_changelog_html():
    """读取 CHANGELOG.md 并渲染为 HTML，供关于页面展示。"""
    path = os.path.join(app.root_path, 'CHANGELOG.md')
    if not os.path.exists(path):
        return ''
    with open(path, 'r', encoding='utf-8') as f:
        return _render_changelog_markdown(f.read())


@app.route('/about')
@login_required
def about():
    """关于页面：展示版本信息、GitHub 仓库入口与更新日志。"""
    return render_template('about.html', title="关于", changelog_html=_load_changelog_html())


@app.route('/image/<item_id>')
@login_required
def proxy_image(item_id):
    """代理获取 Jellyfin 的图片流，解决跨域或者直接访问受限的问题。"""
    img_url = f"{current_user.jellyfin_url}/Items/{item_id}/Images/Primary?fillHeight=450&fillWidth=300&quality=90"
    headers = {"X-Emby-Token": current_user.jellyfin_api_key}
    try:
        resp = requests.get(img_url, headers=headers, stream=True, timeout=10)
        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        return Response(resp.iter_content(chunk_size=1024), content_type=content_type)
    except Exception as e:
        logger.warning(f"[图片代理] 代理图片失败 (item_id={item_id}): {str(e)}")
        return "Not found", 404


@app.route('/logout')
@login_required
def logout():
    """处理用户退出登录并重定向。"""
    username = current_user.username
    logout_user()
    logger.info(f"[登录] 用户 {username} 已退出登录")
    return redirect(url_for('login'))


def format_jellyfin_date(date_str):
    """把 Jellyfin 传过来的 UTC 时间转成咱们自己熟悉的东八区时间，用来页面显示。"""
    if not date_str:
        return "未知时间"
    try:
        date_str = date_str.split('.')[0]
        dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
        dt_local = dt + timedelta(hours=8)
        return dt_local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return date_str


def parse_jellyfin_date(date_raw):
    """把 Jellyfin 时间字符串处理成 Python 的 datetime 对象用于存库对比。"""
    if not date_raw:
        return None
    date_str = date_raw.split('.')[0]
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
        return dt + timedelta(hours=8)
    except ValueError:
        return None


def download_image(url, headers, local_path, session=None):
    """一个通用的图片下载工具，如果在本地找到这张图了就跳过，省点带宽。"""
    if os.path.exists(local_path):
        return True
    try:
        http_get = session.get if session else requests.get
        resp = http_get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            with open(local_path, 'wb') as f:
                f.write(resp.content)
            return True
    except Exception:
        pass
    return False


def get_tmdb_id_smart(user, item, item_type, tmdb_cache, session=None):
    """优先从 Jellyfin 刮削的数据里找 TMDB ID，要找不着就去 TMDB 官方接口里用中文名字再搜一遍兜底。"""
    if item_type == "Movie":
        tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
        query_title = item.get("Name")
        search_type = "movie"
        year = item.get("ProductionYear")
    else:
        tmdb_id = item.get("SeriesProviderIds", {}).get("Tmdb")
        query_title = item.get("SeriesName")
        search_type = "tv"
        year = None

    if tmdb_id:
        return str(tmdb_id)

    if not query_title or not user.tmdb_api_key:
        return None

    cache_key = f"{search_type}_{query_title}"
    if cache_key in tmdb_cache:
        return tmdb_cache[cache_key]

    try:
        http_get = session.get if session else requests.get
        url = f"https://api.themoviedb.org/3/search/{search_type}"
        params = {
            "api_key": user.tmdb_api_key,
            "query": query_title,
            "language": "zh-CN",
            "page": 1
        }
        if year:
            if search_type == 'movie':
                params['primary_release_year'] = year
            else:
                params['first_air_date_year'] = year

        resp = http_get(url, params=params, proxies=get_user_proxies(user), timeout=5)
        if resp.status_code == 200:
            results = resp.json().get('results', [])
            if results:
                fetched_id = str(results[0].get('id'))
                tmdb_cache[cache_key] = fetched_id
                return fetched_id
    except Exception as e:
        logger.warning(f"[TMDB] ID 嗅探请求失败 (关键字: {query_title}): {str(e)}")
        pass

    tmdb_cache[cache_key] = None
    return None


def update_watch_record(user_id, item, item_type, lib_name, dt_local, tmdb_id):
    """写入或更新观看流水账记录。发现用户重新看了一遍被软删除过的记录时，会给它“复活”。"""
    item_id = item["Id"]

    record = WatchRecord.query.filter_by(user_id=user_id, item_id=item_id).first()

    if not record:
        record = WatchRecord(
            user_id=user_id, item_id=item_id, item_type=item_type, library_name=lib_name,
            title=item.get("Name", "未知"), date_played=dt_local, source="Jellyfin", tmdb_id=tmdb_id
        )
        if item_type == "Episode":
            if item_type == "Episode":
                record.series_name = item.get("SeriesName", "未知剧集")

                p_index = item.get("ParentIndexNumber")
                if item.get("SeasonName"):
                    record.season_name = item.get("SeasonName")
                elif p_index is not None:
                    record.season_name = f"第 {p_index} 季" if p_index != 0 else "特别篇"
                else:
                    record.season_name = "第 1 季"

                ep_index = item.get('IndexNumber')
                record.title = f"第 {ep_index or '?'} 集 - {item.get('Name', '未知集名')}"
                try:
                    record.episode_num = int(ep_index) if ep_index is not None else None
                except (ValueError, TypeError):
                    record.episode_num = None

        db.session.add(record)
        return True
    else:
        updated = False

        if getattr(record, 'is_deleted', False):
            if dt_local > record.date_played:
                record.is_deleted = False
                record.date_played = dt_local
                updated = True
        elif dt_local > record.date_played:
            record.date_played = dt_local
            updated = True

        return updated


def update_watch_poster(user_id, jf_user_id, item, item_type, dt_local, jf_url, headers, poster_dir, backdrop_dir,
                        synced_names, tmdb_id, poster_cache, session=None):
    """更新海报墙数据的双缓存，确保存下来的海报图和基本信息都是最新的。"""
    http_get = session.get if session else requests.get
    if item_type == "Movie":
        target_id = item["Id"]
        display_title = item["Name"]
        pure_series_name = None
        season_num_int = None
    elif item_type == "Episode":
        pure_series_name = item.get("SeriesName", "未知剧集")
        season_name = item.get("SeasonName", "未知季")
        try:
            season_num_int = int(item.get("ParentIndexNumber")) if item.get("ParentIndexNumber") is not None else 1
        except ValueError:
            season_num_int = 1

        target_id = f"{item.get('SeriesId', 'unknown')}_S{season_num_int}"
        if season_name != "未知季" and season_name != pure_series_name:
            display_title = f"{pure_series_name} ({season_name})"
        else:
            display_title = f"{pure_series_name} (第 {season_num_int} 季)"
    else:
        return

    cache_key = f"{user_id}_{target_id}"

    if cache_key in poster_cache:
        poster_record = poster_cache[cache_key]
    else:
        poster_record = WatchPoster.query.filter_by(user_id=user_id, target_id=target_id).first()

    if not poster_record:
        series_relative_path = "images/logo.png"
        season_relative_path = "images/logo.png"

        if item_type == "Movie":
            overview = item.get("Overview") or ""
            season_overview = ""
        else:
            overview = ""
            season_overview = ""

            series_id = item.get("SeriesId")
            if series_id:
                try:
                    s_resp = http_get(f"{jf_url}/Users/{jf_user_id}/Items/{series_id}?Fields=Overview",
                                      headers=headers, timeout=5)
                    if s_resp.status_code == 200:
                        overview = s_resp.json().get("Overview") or ""
                except Exception:
                    pass

            season_id = item.get("SeasonId")
            if season_id:
                try:
                    se_resp = http_get(f"{jf_url}/Users/{jf_user_id}/Items/{season_id}?Fields=Overview",
                                       headers=headers, timeout=5)
                    if se_resp.status_code == 200:
                        season_overview = se_resp.json().get("Overview") or ""
                except Exception:
                    pass

        bg_source_id = item.get("SeriesId") if item_type == "Episode" else item["Id"]

        backdrop_filename = f"{bg_source_id}_backdrop.jpg"
        backdrop_path = os.path.join(backdrop_dir, backdrop_filename)
        backdrop_relative_path = f"backdrops/{backdrop_filename}"
        if not download_image(f"{jf_url}/Items/{bg_source_id}/Images/Backdrop/0?maxWidth=1920&quality=85", headers,
                              backdrop_path, session):
            backdrop_relative_path = None

        background_filename = f"{bg_source_id}_background.jpg"
        background_path = os.path.join(backdrop_dir, background_filename)
        background_relative_path = f"backdrops/{background_filename}"
        if not download_image(f"{jf_url}/Items/{bg_source_id}/Images/Backdrop/1?maxWidth=1920&quality=85", headers,
                              background_path, session):
            background_relative_path = None

        if item_type == "Movie":
            movie_path = os.path.join(poster_dir, f"{target_id}_main.jpg")
            series_relative_path = f"posters/{target_id}_main.jpg"
            if not download_image(f"{jf_url}/Items/{target_id}/Images/Primary?maxWidth=400", headers, movie_path,
                                  session):
                series_relative_path = "images/logo.png"
        else:
            series_id = item.get("SeriesId")
            series_path = os.path.join(poster_dir, f"{series_id}_main.jpg")
            series_relative_path = f"posters/{series_id}_main.jpg"
            if not download_image(f"{jf_url}/Items/{series_id}/Images/Primary?maxWidth=400", headers, series_path,
                                  session):
                series_relative_path = "images/logo.png"

            season_id = item.get("SeasonId")
            season_path = os.path.join(poster_dir, f"{target_id}_season.jpg")
            season_relative_path = f"posters/{target_id}_season.jpg"
            if season_id and download_image(f"{jf_url}/Items/{season_id}/Images/Primary?maxWidth=400", headers,
                                            season_path, session):
                pass
            else:
                season_relative_path = series_relative_path

        poster_record = WatchPoster(
            user_id=user_id, target_id=target_id, media_type="Series" if item_type == "Episode" else "Movie",
            display_title=display_title, series_name=pure_series_name, season_num=season_num_int,
            local_image_path=season_relative_path if item_type == "Episode" else series_relative_path,
            series_image_path=series_relative_path, backdrop_image_path=backdrop_relative_path,
            background_image_path=background_relative_path,
            overview=overview, season_overview=season_overview, last_watched_date=dt_local, tmdb_id=tmdb_id
        )
        db.session.add(poster_record)
        synced_names.add(display_title)

        poster_cache[cache_key] = poster_record
    else:
        poster_cache[cache_key] = poster_record

        if getattr(poster_record, 'is_deleted', False):
            if dt_local > poster_record.last_watched_date:
                poster_record.is_deleted = False
                poster_record.last_watched_date = dt_local
                synced_names.add(display_title)

        elif dt_local > poster_record.last_watched_date:
            poster_record.last_watched_date = dt_local


@app.route('/sync_history')
@login_required
def sync_history():
    """手动触发一次对 Jellyfin 观看记录的全量拉取同步。"""
    try:
        result = _run_full_sync(current_user, on_progress=None, use_lock=False, collect_log_names=False)
        if not result['ok']:
            flash("无法获取媒体库列表，同步失败。")
            return redirect(url_for('watched_list'))

        sync_count = result['sync_count']
        synced_names = result['synced_names']

        if sync_count > 0:
            names_str = ", ".join(sorted(synced_names))
            logger.info(f"[手动同步] 用户 {current_user.username} 手动同步完成！新增/更新了 {sync_count} 项记录: {names_str}")

            names_html = "<ul style='margin: 10px 0 0 0; padding-left: 20px; text-align: left; max-height: 150px; overflow-y: auto; color: var(--text-main);'>" + "".join(
                [f"<li style='margin-bottom: 6px;'>{n}</li>" for n in sorted(synced_names)]) + "</ul>"
            flash(f"同步成功！已处理明细并缓存海报/剧照/背景图：{names_html}")
        else:
            logger.info(f"[手动同步] 用户 {current_user.username} 手动同步完成！本地记录已是最新，无新增。")
            flash("同步完成！本地海报及历史记录已是最新。")

    except Exception as e:
        logger.error(f"[手动同步] 媒体库同步过程中发生网络异常: {str(e)}")
        flash(f"同步过程中发生网络异常: {str(e)}")

    return redirect(url_for('watched_list'))


@app.route('/api/sync_stream')
@login_required
def api_sync_stream():
    """配合前端弹窗做的实时的 SSE 接口，把同步进度一点一点地推送到网页上。"""
    # 在请求上下文内先解析出真实的 User 对象，供后台线程使用（代理对象离开请求上下文会失效）
    user = current_user._get_current_object()

    def generate():
        event_queue = Queue()
        result = None
        error_message = None

        try:
            logger.info(f"[实时同步] 用户 {user.username} 触发了前端实时全量同步流...")
            yield f"data: {json.dumps({'status': 'syncing', 'name': '正在请求媒体库列表...'})}\n\n"

            def push_progress(text):
                event_queue.put(f"data: {json.dumps({'status': 'syncing', 'name': text})}\n\n")

            def worker():
                nonlocal result, error_message
                with app.app_context():
                    try:
                        result = _run_full_sync(user, on_progress=push_progress, use_lock=True,
                                                collect_log_names=True)
                    except Exception as e:
                        error_message = str(e)
                        event_queue.put(f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n")
                    finally:
                        event_queue.put(None)

            threading.Thread(target=worker, daemon=True).start()

            while True:
                event = event_queue.get()
                if event is None:
                    break
                yield event

            if error_message:
                logger.error(f"[实时同步] SSE 实时同步流异常终止: {error_message}")
                return

            if not result or not result['ok']:
                yield f"data: {json.dumps({'status': 'error', 'message': '无法获取媒体库列表'})}\n\n"
                return

            sync_count = result['sync_count']
            synced_names = result['synced_names']

            # 把名字拼装成换行的列表格式写入日志
            if sync_count > 0:
                names_str = "\n" + "\n".join([f"  - {name}" for name in sorted(synced_names)])
                logger.info(
                    f"[实时同步] 用户 {user.username} 实时同步完成！共入库/更新了 {sync_count} 项:{names_str}")
            else:
                logger.info(f"[实时同步] 用户 {user.username} 实时同步完成！本地记录已是最新，无新增。")

            yield f"data: {json.dumps({'status': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"
            logger.error(f"[实时同步] SSE 实时同步流异常终止: {str(e)}")

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

def _build_watched_data(user_id):
    """历史记录展示大全，采用双轨引擎：按媒体库展示不去重（保留数据源真实性），按类型展示严格去重。"""
    raw_records = db.session.query(
        WatchRecord.id, WatchRecord.item_type, WatchRecord.title, WatchRecord.series_name,
        WatchRecord.season_name, WatchRecord.tmdb_id, WatchRecord.library_name, WatchRecord.date_played
    ).filter_by(user_id=user_id, is_deleted=False).order_by(WatchRecord.date_played.desc()).all()

    posters = db.session.query(
        WatchPoster.media_type, WatchPoster.display_title, WatchPoster.series_name,
        WatchPoster.tmdb_id, WatchPoster.local_image_path, WatchPoster.series_image_path
    ).filter_by(user_id=user_id, is_deleted=False).all()

    # 构建双维度海报映射字典，极大提高命中率
    movie_poster_map = {}
    movie_tmdb_map = {}
    series_poster_map = {}
    series_tmdb_map = {}

    for p in posters:
        if p.media_type == "Movie":
            movie_poster_map[p.display_title] = p.local_image_path
            if p.tmdb_id:
                movie_tmdb_map[str(p.tmdb_id)] = p.local_image_path
        else:
            path = p.series_image_path or p.local_image_path
            series_poster_map[p.series_name] = path
            if p.tmdb_id:
                series_tmdb_map[str(p.tmdb_id)] = path

    def is_valid_tmdb(tid):
        return bool(tid and str(tid).lower() not in ['none', 'null', ''])

    library_data = {}
    type_data = {
        '电影区': {'episodes_tree': {}, 'movies': [], 'series_posters': {}},
        '剧集区': {'episodes_tree': {}, 'movies': [], 'series_posters': {}}
    }

    # ========================================================
    # 引擎 1：构建【按媒体库分组】数据（保留所有真实来源，不去重）
    # ========================================================
    for record in raw_records:
        lib_name = record.library_name or "未分类媒体库"
        item_type = record.item_type
        date_played_str = record.date_played.strftime('%Y-%m-%d %H:%M')

        if lib_name not in library_data:
            library_data[lib_name] = {
                'episodes_tree': {},
                'movies': [],
                'series_posters': {}
            }

        if item_type == "Movie":
            poster_img = "images/logo.png"
            if is_valid_tmdb(record.tmdb_id) and str(record.tmdb_id) in movie_tmdb_map:
                poster_img = movie_tmdb_map[str(record.tmdb_id)]
            else:
                poster_img = movie_poster_map.get(record.title, "images/logo.png")

            movie_node = {
                'id': record.id,
                'name': record.title,
                'date': date_played_str,
                'poster_path': poster_img
            }
            library_data[lib_name]['movies'].append(movie_node)

        else:
            series_name = getattr(record, 'series_name', record.title)
            season_name = getattr(record, 'season_name')
            if not season_name:
                season_name = "第 1 季"

            episode_name = record.title
            poster_img = "images/logo.png"
            if is_valid_tmdb(record.tmdb_id) and str(record.tmdb_id) in series_tmdb_map:
                poster_img = series_tmdb_map[str(record.tmdb_id)]
            else:
                poster_img = series_poster_map.get(series_name, "images/logo.png")

            ep_node = {
                'id': record.id,
                'episode': episode_name,
                'date': date_played_str
            }

            library_data[lib_name]['series_posters'][series_name] = poster_img
            if series_name not in library_data[lib_name]['episodes_tree']:
                library_data[lib_name]['episodes_tree'][series_name] = {}
            if season_name not in library_data[lib_name]['episodes_tree'][series_name]:
                library_data[lib_name]['episodes_tree'][series_name][season_name] = []
            library_data[lib_name]['episodes_tree'][series_name][season_name].append(ep_node)

    # ========================================================
    # 引擎 2：构建【按类型分组】数据（底层字典聚合，严格去重）
    # ========================================================
    unique_records_dict = {}
    for record in raw_records:
        item_type = record.item_type
        tmdb_id = record.tmdb_id
        valid_tmdb = is_valid_tmdb(tmdb_id)

        # 构造去重唯一标识键
        if item_type == "Movie":
            key = f"movie_tmdb_{tmdb_id}" if valid_tmdb else f"movie_title_{record.title}"
        else:
            season_name = getattr(record, 'season_name', '') or "第 1 季"
            episode_title = record.title
            if valid_tmdb:
                key = f"ep_tmdb_{tmdb_id}_{season_name}_{episode_title}"
            else:
                series_name = getattr(record, 'series_name', record.title)
                key = f"ep_title_{series_name}_{season_name}_{episode_title}"

        # 发现重复时，保留 date_played 最早（最小）的记录
        if key not in unique_records_dict:
            unique_records_dict[key] = record
        else:
            if record.date_played < unique_records_dict[key].date_played:
                unique_records_dict[key] = record

    # 提取去重后的数据，再次按照日期倒序排列，保证新看的剧排在海报墙前面
    deduped_records = list(unique_records_dict.values())
    deduped_records.sort(key=lambda x: x.date_played, reverse=True)

    for record in deduped_records:
        item_type = record.item_type
        date_played_str = record.date_played.strftime('%Y-%m-%d %H:%M')

        if item_type == "Movie":
            poster_img = "images/logo.png"
            if is_valid_tmdb(record.tmdb_id) and str(record.tmdb_id) in movie_tmdb_map:
                poster_img = movie_tmdb_map[str(record.tmdb_id)]
            else:
                poster_img = movie_poster_map.get(record.title, "images/logo.png")

            movie_node = {
                'id': record.id,
                'name': record.title,
                'date': date_played_str,
                'poster_path': poster_img
            }
            type_data['电影区']['movies'].append(movie_node)

        else:
            series_name = getattr(record, 'series_name', record.title)
            season_name = getattr(record, 'season_name')
            if not season_name:
                season_name = "第 1 季"

            episode_name = record.title
            poster_img = "images/logo.png"
            if is_valid_tmdb(record.tmdb_id) and str(record.tmdb_id) in series_tmdb_map:
                poster_img = series_tmdb_map[str(record.tmdb_id)]
            else:
                poster_img = series_poster_map.get(series_name, "images/logo.png")

            ep_node = {
                'id': record.id,
                'episode': episode_name,
                'date': date_played_str
            }

            type_data['剧集区']['series_posters'][series_name] = poster_img
            if series_name not in type_data['剧集区']['episodes_tree']:
                type_data['剧集区']['episodes_tree'][series_name] = {}
            if season_name not in type_data['剧集区']['episodes_tree'][series_name]:
                type_data['剧集区']['episodes_tree'][series_name][season_name] = []
            type_data['剧集区']['episodes_tree'][series_name][season_name].append(ep_node)

    final_type_data = {}
    if type_data['电影区']['movies']:
        final_type_data['电影区'] = type_data['电影区']
    if type_data['剧集区']['episodes_tree']:
        final_type_data['剧集区'] = type_data['剧集区']

    return library_data, final_type_data


@app.route('/watched_list')
@login_required
def watched_list():
    """历史记录页：只内联轻量摘要（库名/类型区名），展开时按需请求数据，避免全量 JSON 进页面。"""
    library_data, type_data = _build_watched_data(current_user.id)
    summary = {
        'library_names': list(library_data.keys()),
        'type_names': list(type_data.keys())
    }
    return render_template('watched_list.html', summary=summary)


@app.route('/api/watched_library')
@login_required
def api_watched_library():
    """按媒体库返回观看历史数据（展开某个库时前端调用）"""
    lib = request.args.get('lib', '').strip()
    if not lib:
        return jsonify({})
    library_data, _ = _build_watched_data(current_user.id)
    return jsonify(library_data.get(lib, {}))


@app.route('/api/watched_type')
@login_required
def api_watched_type():
    """返回按类型去重后的观看历史数据（类型视图展开时前端调用）"""
    _, type_data = _build_watched_data(current_user.id)
    return jsonify(type_data)


@app.route('/detail/<media_type>/<path:title>')
@login_required
def media_detail(media_type, title):
    """本地观影记录的详情展示页，用于看一看追剧的明细进度和所有单集的横幅图。"""
    season_poster_map, season_overview_map = {}, {}
    # 从媒体库视图进入时携带 ?lib=，仅展示该媒体库的记录；从类型视图进入时不携带，做同集去重
    lib_filter = request.args.get('lib', '').strip()

    if media_type == 'series':
        poster_info = WatchPoster.query.filter_by(user_id=current_user.id, media_type='Series', series_name=title,
                                                  is_deleted=False).first()
        for p in WatchPoster.query.filter_by(user_id=current_user.id, media_type='Series', series_name=title,
                                             is_deleted=False).all():
            if p.season_num:
                season_poster_map[p.season_num] = p.local_image_path
                season_overview_map[p.season_num] = p.season_overview or ""
    else:
        poster_info = WatchPoster.query.filter_by(user_id=current_user.id, media_type='Movie', display_title=title,
                                                  is_deleted=False).first()

    if not poster_info: poster_info = WatchPoster()

    seasons, movie_record = {}, None

    if media_type == 'series':
        ep_query = WatchRecord.query.filter_by(user_id=current_user.id, item_type='Episode', is_deleted=False)
        if lib_filter:
            ep_query = ep_query.filter_by(library_name=lib_filter)
        ep_records = [r for r in ep_query.all() if getattr(r, 'series_name', r.title) == title]

        # 优化:一次批量查询本剧所有单集详情,替代每集一次 SQL
        ep_ids = [ep.item_id for ep in ep_records]
        ep_detail_map = {}
        if ep_ids:
            ep_detail_map = {d.item_id: d for d in
                             EpisodeDetail.query.filter(EpisodeDetail.item_id.in_(ep_ids)).all()}

        # 类型视图（无 lib 参数）：同一集可能因 Jellyfin 重复条目 / Watcharr 导入等多来源存在多条记录，
        # 按最早观看时间正序后对"同季同集"去重，每集只展示最早的一条（首次观看）；
        # 媒体库视图（带 lib 参数）：仅展示对应媒体库的记录，不去重
        if not lib_filter:
            ep_records.sort(key=lambda x: x.date_played)
        seen_episodes = set()
        for ep in ep_records:
            ep_detail = ep_detail_map.get(ep.item_id)
            real_season_num = 1
            if ep_detail:
                ep.still_path = ep_detail.still_image_path
                ep.overview = ep_detail.overview or ""
                if ep_detail.season_num: real_season_num = ep_detail.season_num
            else:
                ep.still_path, ep.overview = "images/logo.png", ""

            if real_season_num == 1 and getattr(ep, 'season_name', None):
                match = re.search(r'\d+', ep.season_name)
                if match: real_season_num = int(match.group())

            if not lib_filter:
                # 去重键：同季同集合并；集数为空时按 item_id 保留原样
                ep_key = (real_season_num, ep.episode_num if ep.episode_num is not None else ep.item_id)
                if ep_key in seen_episodes:
                    continue
                seen_episodes.add(ep_key)

            if real_season_num not in seasons: seasons[real_season_num] = []
            seasons[real_season_num].append(ep)
        seasons = dict(sorted(seasons.items()))
        for s in seasons:
            seasons[s].sort(key=lambda x: x.episode_num if x.episode_num is not None else 9999)
    else:
        movie_query = WatchRecord.query.filter_by(user_id=current_user.id, item_type='Movie', title=title,
                                                  is_deleted=False)
        if lib_filter:
            movie_query = movie_query.filter_by(library_name=lib_filter)
        movie_record = movie_query.first()

    return render_template('detail.html', media_type=media_type, title=title, poster=poster_info, seasons=seasons,
                           movie_record=movie_record, season_poster_map=season_poster_map,
                           season_overview_map=season_overview_map)


def get_user_proxies(user):
    """读取用户在系统里配置的代理选项，组装成可以喂给 requests 的代理参数格式。"""
    if user.proxy_url and user.proxy_port:
        proxy_addr = f"http://{user.proxy_url}:{user.proxy_port}"
        return {"http": proxy_addr, "https": proxy_addr}
    return None


@app.route('/api/delete_history', methods=['POST'])
@login_required
def delete_history():
    """批量软删除指定的观影历史记录，并且会顺带做检测，如果某部剧看完的集数全被删了，就把对应的海报也给一起隐藏掉。"""
    data = request.json
    record_ids = data.get('record_ids', [])
    if not record_ids:
        return jsonify({'success': False, 'message': '未选择任何记录'})

    try:
        records = WatchRecord.query.filter(WatchRecord.id.in_(record_ids), WatchRecord.user_id == current_user.id).all()

        movies_to_check = set()
        series_to_check = set()

        for r in records:
            r.is_deleted = True
            if r.item_type == 'Movie':
                movies_to_check.add(r.title)
            else:
                series_to_check.add(getattr(r, 'series_name', r.title))

        db.session.flush()

        for m_title in movies_to_check:
            if not WatchRecord.query.filter_by(user_id=current_user.id, item_type='Movie', title=m_title,
                                               is_deleted=False).first():
                for p in WatchPoster.query.filter_by(user_id=current_user.id, media_type='Movie',
                                                     display_title=m_title).all():
                    p.is_deleted = True

        for s_name in series_to_check:
            if not WatchRecord.query.filter_by(user_id=current_user.id, item_type='Episode', series_name=s_name,
                                               is_deleted=False).first():
                for p in WatchPoster.query.filter_by(user_id=current_user.id, media_type='Series',
                                                     series_name=s_name).all():
                    p.is_deleted = True

        db.session.commit()
        logger.info(f"[历史管理] 用户 {current_user.username} 手动删除了 {len(records)} 条观影足迹及相关缓存。")
        return jsonify({'success': True, 'message': f'成功移除了 {len(records)} 条足迹'})
    except Exception as e:
        db.session.rollback()
        logger.error(f"[历史管理] 删除观影记录失败 (用户: {current_user.username}): {str(e)}")
        return jsonify({'success': False, 'message': f'删除失败: {str(e)}'})


@app.route('/api/update_history_date', methods=['POST'])
@login_required
def update_history_date():
    """接收前台的时间修改请求，批量修改观影记录的时间，并且联动着把海报墙的排序时间也顺手改一下。"""
    data = request.json
    record_ids = data.get('record_ids', [])
    new_date_str = data.get('new_date')

    if not record_ids or not new_date_str:
        return jsonify({'success': False, 'message': '未选择记录或未提供时间'})

    try:
        new_date = datetime.strptime(new_date_str, '%Y-%m-%dT%H:%M')

        records = WatchRecord.query.filter(WatchRecord.id.in_(record_ids), WatchRecord.user_id == current_user.id).all()

        movies_to_update = set()
        series_to_update = set()

        for r in records:
            r.date_played = new_date
            if r.item_type == 'Movie':
                movies_to_update.add(r.title)
            else:
                series_to_update.add(getattr(r, 'series_name', r.title))

        db.session.flush()

        for m_title in movies_to_update:
            for p in WatchPoster.query.filter_by(user_id=current_user.id, media_type='Movie',
                                                 display_title=m_title).all():
                p.last_watched_date = new_date

        for s_name in series_to_update:
            for p in WatchPoster.query.filter_by(user_id=current_user.id, media_type='Series',
                                                 series_name=s_name).all():
                p.last_watched_date = new_date

        db.session.commit()
        logger.info(f"[历史管理] 用户 {current_user.username} 批量修改了 {len(records)} 条观影足迹的时间。")
        return jsonify({'success': True, 'message': f'成功修改了 {len(records)} 项的时间！'})
    except Exception as e:
        db.session.rollback()
        logger.error(f"[历史管理] 批量修改观影时间失败 (用户: {current_user.username}): {str(e)}")
        return jsonify({'success': False, 'message': f'修改失败: {str(e)}'})


def restore_missing_images_task(app_context, user_id):
    """
    在后台默默跑的图片补全引擎。
    每次导完数据后如果发现本地的 static 文件夹里丢图了，它就会自己去 TMDB 重新下载，遇到报错也会精准记录日志。
    """
    with app_context:
        user = load_user(user_id)
        if not user or not user.tmdb_api_key:
            logger.warning(f"[补全引擎] 用户 {user.username} 未配置 TMDB API Key，自动跳过图片补全任务。")
            return

        logger.info(f"[补全引擎] 启动任务：开始为用户 {user.username} 扫描并补全丢失的本地海报和剧照...")
        proxies = get_user_proxies(user)
        static_dir = os.path.join(app.root_path, 'static')

        download_count = 0
        import time

        def fetch_with_retry(url, params=None, timeout=30, retries=3):
            """内部下载工具封装，带重试机制，搞定网络时不时抽风的问题。"""
            for attempt in range(retries):
                try:
                    resp = requests.get(url, params=params, proxies=proxies, timeout=timeout)
                    if resp.status_code == 200:
                        return resp
                except Exception as e:
                    if attempt == retries - 1:
                        raise e
                    time.sleep(2)
            return None

        def download_exact(url, relative_path):
            """判断图片是否存在并进行物理下载。"""
            nonlocal download_count
            if not url or not relative_path or relative_path == "images/logo.png": return False
            filepath = os.path.join(static_dir, relative_path)
            if os.path.exists(filepath): return False

            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            resp = fetch_with_retry(url)
            if resp:
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                download_count += 1
                return True
            return False

        posters = WatchPoster.query.filter_by(user_id=user_id).all()
        for p in posters:
            if not p.tmdb_id: continue

            needs_main = p.series_image_path and p.series_image_path != "images/logo.png" and not os.path.exists(
                os.path.join(static_dir, p.series_image_path))
            needs_local = p.local_image_path and p.local_image_path != "images/logo.png" and not os.path.exists(
                os.path.join(static_dir, p.local_image_path))
            needs_backdrop = p.backdrop_image_path and not os.path.exists(
                os.path.join(static_dir, p.backdrop_image_path))

            if not (needs_main or needs_local or needs_backdrop):
                continue

            base_name = p.series_name if p.media_type == 'Series' and p.series_name else p.display_title
            season_text = f"第 {p.season_num} 季" if p.season_num is not None and p.season_num > 0 else "特别篇"

            tmdb_type = 'movie' if p.media_type == 'Movie' else 'tv'
            url = f"https://api.themoviedb.org/3/{tmdb_type}/{p.tmdb_id}"

            try:
                resp = fetch_with_retry(url, params={"api_key": user.tmdb_api_key, "language": "zh-CN"})
                if not resp: continue
                data = resp.json()
            except Exception as e:
                logger.error(f"[补全引擎] {base_name} 元数据拉取失败: {str(e)}")
                continue

            if needs_main:
                try:
                    remote = data.get('poster_path')
                    if remote and download_exact(
                            f"https://image.tmdb.org/t/p/w500{remote}",
                            p.series_image_path):
                        logger.info(f"[补全引擎] {base_name} 主海报补全下载成功")
                except Exception as e:
                    logger.error(f"[补全引擎] {base_name} 主海报补全失败: {str(e)}")

            if needs_backdrop:
                try:
                    remote = data.get('backdrop_path')
                    if remote and download_exact(
                            f"https://image.tmdb.org/t/p/w1280{remote}",
                            p.backdrop_image_path):
                        logger.info(f"[补全引擎] {base_name} 背景图补全下载成功")
                except Exception as e:
                    logger.error(f"[补全引擎] {base_name} 背景图补全失败: {str(e)}")

            if needs_local:
                try:
                    if p.media_type == 'Movie':
                        remote = data.get('poster_path')
                        if remote and download_exact(
                                f"https://image.tmdb.org/t/p/w500{remote}",
                                p.local_image_path):
                            logger.info(f"[补全引擎] {base_name} 主海报补全下载成功")
                    elif p.season_num is not None:
                        s_url = f"https://api.themoviedb.org/3/tv/{p.tmdb_id}/season/{p.season_num}"
                        s_resp = fetch_with_retry(s_url, params={"api_key": user.tmdb_api_key, "language": "zh-CN"})
                        if s_resp:
                            remote = s_resp.json().get('poster_path')
                            if remote and download_exact(
                                    f"https://image.tmdb.org/t/p/w500{remote}",
                                    p.local_image_path):
                                logger.info(f"[补全引擎] {base_name} {season_text}海报补全下载成功")
                except Exception as e:
                    if p.media_type == 'Movie':
                        logger.error(f"[补全引擎] {base_name} 主海报补全失败: {str(e)}")
                    else:
                        logger.error(f"[补全引擎] {base_name} {season_text}海报补全失败: {str(e)}")

        user_ep_ids = [r.item_id for r in WatchRecord.query.filter_by(user_id=user_id, item_type='Episode').all()]
        if user_ep_ids:
            ep_details = EpisodeDetail.query.filter(EpisodeDetail.item_id.in_(user_ep_ids)).all()
            for ed in ep_details:
                if not ed.series_tmdb_id or ed.season_num is None or ed.episode_num is None: continue
                if ed.still_image_path and ed.still_image_path != "images/logo.png" and not os.path.exists(
                        os.path.join(static_dir, ed.still_image_path)):
                    ep_url = f"https://api.themoviedb.org/3/tv/{ed.series_tmdb_id}/season/{ed.season_num}/episode/{ed.episode_num}"
                    ep_season_text = f"第 {ed.season_num} 季" if ed.season_num > 0 else "特别篇"
                    try:
                        ep_resp = fetch_with_retry(ep_url, params={"api_key": user.tmdb_api_key, "language": "zh-CN"})
                        if ep_resp:
                            remote = ep_resp.json().get('still_path')
                            if remote and download_exact(
                                    f"https://image.tmdb.org/t/p/w300{remote}",
                                    ed.still_image_path):
                                logger.info(
                                    f"[补全引擎] {ed.series_name} {ep_season_text} 第 {ed.episode_num} 集剧照补全下载成功")
                    except Exception as e:
                        logger.error(
                            f"[补全引擎] {ed.series_name} {ep_season_text} 第 {ed.episode_num} 集剧照补全失败: {str(e)}")

        logger.info(
            f"[补全引擎] 任务结束：用户 {user.username} 的缺失图片扫描完毕，共成功下载补全了 {download_count} 张图片文件。")


@app.route('/export_data')
@login_required
def export_data():
    """把用户看过的记录、海报和剧照缓存全都导出成一份干净的 JSON 文件用来备份。"""

    logger.info(f"[数据导出] 用户 {current_user.username} 发起了纯净历史数据导出请求（排除配置信息）。")

    try:
        records = WatchRecord.query.filter_by(user_id=current_user.id).all()
        records_list = []
        user_ep_item_ids = []
        for r in records:
            if r.item_type == 'Episode': user_ep_item_ids.append(r.item_id)
            records_list.append({
                "item_id": r.item_id,
                "item_type": r.item_type,
                "library_name": r.library_name,
                "title": r.title,
                "series_name": r.series_name,
                "season_name": r.season_name,
                "episode_num": r.episode_num,
                "source": r.source,
                "tmdb_id": r.tmdb_id,
                "date_played": r.date_played.strftime('%Y-%m-%d %H:%M:%S') if r.date_played else None,
                "is_deleted": r.is_deleted
            })

        posters = WatchPoster.query.filter_by(user_id=current_user.id).all()
        posters_list = []
        for p in posters:
            posters_list.append({
                "target_id": p.target_id,
                "media_type": p.media_type,
                "display_title": p.display_title,
                "series_name": p.series_name,
                "season_num": p.season_num,
                "tmdb_id": p.tmdb_id,
                "local_image_path": p.local_image_path,
                "series_image_path": p.series_image_path,
                "backdrop_image_path": p.backdrop_image_path,
                "background_image_path": p.background_image_path,
                "overview": p.overview,
                "season_overview": p.season_overview,
                "last_watched_date": p.last_watched_date.strftime('%Y-%m-%d %H:%M:%S') if p.last_watched_date else None,
                "is_deleted": p.is_deleted
            })

        ep_details = EpisodeDetail.query.filter(
            EpisodeDetail.item_id.in_(user_ep_item_ids)).all() if user_ep_item_ids else []
        ep_details_list = []
        for ed in ep_details:
            ep_details_list.append({
                "item_id": ed.item_id,
                "series_name": ed.series_name,
                "season_num": ed.season_num,
                "episode_num": ed.episode_num,
                "episode_name": ed.episode_name,
                "overview": ed.overview,
                "series_tmdb_id": ed.series_tmdb_id,
                "still_image_path": ed.still_image_path
            })

        export_dict = {
            "version": "1.2",
            "export_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "watch_records": records_list,
            "watch_posters": posters_list,
            "episode_details": ep_details_list
        }

        json_str = json.dumps(export_dict, ensure_ascii=False, indent=2)
        filename = f"jellywall_export_{current_user.username}_{datetime.now().strftime('%Y%m%d%H%M')}.json"

        logger.info(
            f"[数据导出] 成功提取用户 {current_user.username} 的 {len(records_list)} 条足迹，{len(posters_list)} 张海报记录及 {len(ep_details_list)} 集详情。")
        return Response(json_str, mimetype="application/json",
                        headers={"Content-disposition": f"attachment; filename={filename}"})

    except Exception as e:
        logger.error(f"[数据导出] 用户 {current_user.username} 数据提取异常: {str(e)}")
        flash("系统数据打包时发生错误，请联系管理员或查看日志。")
        return redirect(url_for('config'))


@app.route('/api/import_data', methods=['POST'])
@login_required
def import_data():
    """解析前台上传的 JSON 备份数据，只拿里面的观影历史来和现在的数据库做合并，顺带唤醒后台跑一下补全图片的任务。"""
    logger.info(f"[数据导入] 收到来自用户 {current_user.username} 的数据导入请求。")

    if 'file' not in request.files:
        logger.warning(f"[数据导入] 用户 {current_user.username} 上传失败：未收到文件流。")
        return jsonify({"success": False, "message": "未找到上传的文件"})

    file = request.files['file']
    if file.filename == '':
        logger.warning(f"[数据导入] 用户 {current_user.username} 上传失败：提交了空文件。")
        return jsonify({"success": False, "message": "未选择文件"})

    try:
        data = json.loads(file.read().decode('utf-8'))

        logger.info(
            f"[数据导入] 文件解析成功，准备为 {current_user.username} 合并 {len(data.get('watch_records', []))} 条观影记录（跳过用户配置项，仅导入历史数据）。")

        for r_data in data.get("watch_records", []):
            item_id = r_data.get("item_id")
            if not item_id: continue
            rec = WatchRecord.query.filter_by(user_id=current_user.id, item_id=item_id).first()
            dt_played = datetime.strptime(r_data["date_played"], '%Y-%m-%d %H:%M:%S') if r_data.get(
                "date_played") else datetime.now()
            if not rec:
                rec = WatchRecord(user_id=current_user.id, item_id=item_id)
                db.session.add(rec)
            rec.item_type = r_data.get("item_type")
            rec.library_name = r_data.get("library_name", "导入数据")
            rec.title = r_data.get("title", "未知")
            rec.series_name = r_data.get("series_name")
            rec.season_name = r_data.get("season_name")
            rec.episode_num = r_data.get("episode_num")
            rec.source = r_data.get("source", "import")
            rec.tmdb_id = r_data.get("tmdb_id")
            rec.date_played = dt_played
            rec.is_deleted = r_data.get("is_deleted", False)

        for p_data in data.get("watch_posters", []):
            target_id = p_data.get("target_id")
            if not target_id: continue
            pos = WatchPoster.query.filter_by(user_id=current_user.id, target_id=target_id).first()
            dt_last = datetime.strptime(p_data["last_watched_date"], '%Y-%m-%d %H:%M:%S') if p_data.get(
                "last_watched_date") else datetime.now()
            if not pos:
                pos = WatchPoster(user_id=current_user.id, target_id=target_id)
                db.session.add(pos)
            pos.media_type = p_data.get("media_type")
            pos.display_title = p_data.get("display_title", "未知")
            pos.series_name = p_data.get("series_name")
            pos.season_num = p_data.get("season_num")
            pos.tmdb_id = p_data.get("tmdb_id")
            pos.local_image_path = p_data.get("local_image_path", "images/logo.png")
            pos.series_image_path = p_data.get("series_image_path")
            pos.backdrop_image_path = p_data.get("backdrop_image_path")
            pos.background_image_path = p_data.get("background_image_path")
            pos.overview = p_data.get("overview")
            pos.season_overview = p_data.get("season_overview")
            pos.last_watched_date = dt_last
            pos.is_deleted = p_data.get("is_deleted", False)

        for ed_data in data.get("episode_details", []):
            item_id = ed_data.get("item_id")
            if not item_id: continue
            ed = EpisodeDetail.query.filter_by(item_id=item_id).first()
            if not ed:
                ed = EpisodeDetail(item_id=item_id)
                db.session.add(ed)
            ed.series_name = ed_data.get("series_name")
            ed.season_num = ed_data.get("season_num")
            ed.episode_num = ed_data.get("episode_num")
            ed.episode_name = ed_data.get("episode_name")
            ed.overview = ed_data.get("overview")
            ed.series_tmdb_id = ed_data.get("series_tmdb_id")
            ed.still_image_path = ed_data.get("still_image_path")

        db.session.commit()

        logger.info(f"[数据导入] 数据库历史记录合并提交成功。已为 {current_user.username} 触发后台 TMDB 图片补全线程。")

        threading.Thread(target=restore_missing_images_task, args=(app.app_context(), current_user.id)).start()

        return jsonify(
            {"success": True, "message": "导入完成！系统仅合并了历史记录，后台正在自动校验并补全缺失的图片..."})

    except Exception as e:
        db.session.rollback()
        logger.error(f"[数据导入] 用户 {current_user.username} 文件结构解析失败或入库异常: {str(e)}")
        return jsonify({"success": False, "message": f"导入失败，数据格式可能不正确: {str(e)}"})





@app.route('/api/parse_watcharr', methods=['POST'])
@login_required
def parse_watcharr():
    """解析 Watcharr 数据，纳秒兼容，并精准提取 customDate 真实观看时间"""
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "未找到文件"})

    try:
        data = json.loads(request.files['file'].read().decode('utf-8'))
        parsed_results = {}

        now = datetime.now()
        local_tz = now.astimezone().tzinfo

        def parse_date(date_str):
            """纳秒级安全的时间解析器"""
            if not date_str: return now
            try:
                date_str = str(date_str).strip().replace("Z", "+00:00")
                # 核心修复：强行截断超过 6 位的纳秒，防止 Python fromisoformat 崩溃
                if '.' in date_str:
                    base, rest = date_str.split('.', 1)
                    if '+' in rest:
                        frac, tz = rest.split('+', 1)
                        tz = '+' + tz
                    elif '-' in rest:
                        frac, tz = rest.split('-', 1)
                        tz = '-' + tz
                    else:
                        frac = rest
                        tz = ''
                    frac = frac[:6]  # 仅保留最大 6 位微秒
                    date_str = f"{base}.{frac}{tz}"

                dt = datetime.fromisoformat(date_str)
                # 统一转为本地时区去除了 tzinfo，适配 SQLite
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(local_tz).replace(tzinfo=None)
            except Exception as e:
                logger.error(f"[时间解析] 无法解析时间 {date_str}: {e}")
                return now

        for item in data:
            content = item.get('content', {})
            tmdb_id = content.get('tmdbId') or item.get('tmdbId') or ''
            item_type = str(content.get('type') or item.get('type') or '').lower()
            title = content.get('title') or content.get('name') or item.get('title') or item.get('name') or '未知媒体'
            status = str(item.get('status', '')).upper()

            has_tmdb = bool(tmdb_id and str(tmdb_id).lower() not in ['none', 'null', ''])
            core_id = str(
                tmdb_id) if has_tmdb else f"custom_{hashlib.md5((str(item.get('id', '')) or title).encode('utf-8')).hexdigest()[:8]}"

            # 获取主兜底时间
            main_time = parse_date(item.get('updatedAt') or item.get('createdAt'))

            if core_id not in parsed_results:
                parsed_results[core_id] = {
                    "core_id": core_id, "tmdb_id": tmdb_id if has_tmdb else "",
                    "type": item_type, "title": title, "records": []
                }

            if item_type == 'movie':
                if status in ['FINISHED', 'COMPLETED']:
                    movie_time = main_time
                    activities = item.get('activity', [])
                    # ✨ 提取电影的真实历史观看时间
                    for act in activities:
                        if 'WATCHED' in act.get('type', ''):
                            best_date_str = act.get('customDate') or act.get('createdAt')
                            if best_date_str:
                                movie_time = parse_date(best_date_str)
                                if act.get('customDate'):
                                    break  # 只要找到了 customDate 真实时间就跳出

                    parsed_results[core_id]['records'].append({
                        "season": "", "episode": "", "label": title,
                        "watch_date": movie_time.isoformat()
                    })

            elif item_type == 'tv':
                fully_watched_seasons = set(s for s in item.get('watchedSeasons', []) if isinstance(s, int))
                activities = sorted(item.get('activity', []), key=lambda x: x.get('createdAt', ''))

                watched_episodes = {}
                season_added_times = {}

                for act in activities:
                    act_type = act.get('type', '')
                    # ✨ 核心修复：优先抓取 customDate 真实导入时间
                    date_str = act.get('customDate') or act.get('createdAt')
                    act_time = parse_date(date_str)

                    if 'EPISODE_' in act_type:
                        try:
                            act_data = json.loads(act.get('data', '{}'))
                            s, e = act_data.get('season'), act_data.get('episode')
                            if s is not None and e is not None:
                                s, e = int(s), int(e)
                                if 'ADDED' in act_type:
                                    watched_episodes[(s, e)] = act_time
                                elif 'REMOVED' in act_type:
                                    watched_episodes.pop((s, e), None)
                        except:
                            pass

                    elif 'SEASON_ADDED' in act_type:
                        try:
                            act_data = json.loads(act.get('data', '{}'))
                            s = act_data.get('season')
                            if s is not None:
                                season_added_times[int(s)] = act_time
                        except:
                            pass

                if not fully_watched_seasons and not watched_episodes and status in ['FINISHED', 'COMPLETED']:
                    total_seasons = content.get('numberOfSeasons', 0)
                    if isinstance(total_seasons, int) and total_seasons > 0:
                        fully_watched_seasons.update(range(1, total_seasons + 1))
                    else:
                        fully_watched_seasons.add(1)

                # 处理“全季”
                for s_num in fully_watched_seasons:
                    specific_times = {}
                    for (s, e), ep_time in watched_episodes.items():
                        if s == s_num:
                            specific_times[str(e)] = ep_time.isoformat()

                    season_time = season_added_times.get(s_num, main_time)

                    parsed_results[core_id]['records'].append({
                        "season": s_num, "episode": "全季", "label": f"{title} 第{s_num}季 (全)",
                        "watch_date": season_time.isoformat(),
                        "specific_times": specific_times
                    })

                # 处理未全部看完的零散单集
                filtered_episodes = {k: v for k, v in watched_episodes.items() if k[0] not in fully_watched_seasons}
                for (s_num, e_num), ep_time in filtered_episodes.items():
                    parsed_results[core_id]['records'].append({
                        "season": s_num, "episode": e_num, "label": f"{title} S{s_num:02d}E{e_num:02d}",
                        "watch_date": ep_time.isoformat()
                    })

        final_list = [v for v in parsed_results.values() if v['records']]
        final_list.sort(key=lambda x: (0 if x['type'] == 'movie' else 1, x['title']))
        for item in final_list:
            if item['type'] == 'tv':
                item['records'].sort(key=lambda x: (x['season'], -1 if x['episode'] == '全季' else x['episode']))

        return jsonify({"status": "success", "data": final_list})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})





@app.route('/api/check_missing_data', methods=['GET'])
@login_required
def check_missing_data():
    """全盘扫描：揪出所有缺少 TMDB ID、海报路径错误 (双字段校验)、或物理文件丢失的数据"""
    try:
        all_items = WatchPoster.query.filter_by(user_id=current_user.id, is_deleted=False).all()

        results = []
        for item in all_items:
            reasons = []

            # 1. 检查 TMDB ID 是否有效
            valid_tmdb = bool(item.tmdb_id and str(item.tmdb_id).strip().lower() not in ['none', 'null', '', '0'])
            if not valid_tmdb:
                reasons.append("缺失 TMDB ID")

            # 2. ✨ 升级版：联合检查 local_image_path 和 series_image_path 是否为占位图
            img_path = item.local_image_path
            series_img_path = item.series_image_path

            is_missing_poster = False
            # 检查主路径
            if not img_path or 'logo.png' in img_path:
                is_missing_poster = True
            # 如果是剧集，额外检查剧集专属路径
            elif item.media_type == 'Series' and (not series_img_path or 'logo.png' in series_img_path):
                is_missing_poster = True

            if is_missing_poster:
                reasons.append("缺失海报图片")
            else:
                # 3. 检查物理文件是否真实存在于硬盘上
                full_path = os.path.join(app.root_path, 'static', img_path.strip('/'))
                if not os.path.exists(full_path):
                    reasons.append("海报物理文件丢失")
                elif item.media_type == 'Series' and series_img_path:
                    # 同步核查剧集图片的物理文件
                    full_series_path = os.path.join(app.root_path, 'static', series_img_path.strip('/'))
                    if not os.path.exists(full_series_path):
                        reasons.append("剧集海报物理文件丢失")

            # 如果有任何缺失理由，就加入急救名单
            if reasons:
                results.append({
                    "id": item.id,
                    "title": item.display_title,
                    "type": "电影" if item.media_type == "Movie" else "剧集",
                    "raw_type": item.media_type,
                    "reason": " & ".join(reasons)
                })

        if results:
            logger.warning(
                f"[数据检查] 用户 {current_user.username} 扫描完成：共检查 {len(all_items)} 条，发现 {len(results)} 条缺失")
        else:
            logger.info(
                f"[数据检查] 用户 {current_user.username} 扫描完成：共检查 {len(all_items)} 条，未发现缺失")
        return jsonify({"status": "success", "data": results})
    except Exception as e:
        logger.error(f"[数据检查] 扫描缺失数据异常: {e}")
        return jsonify({"status": "error", "message": "扫描数据库异常"})




@app.route('/api/execute_data_completion', methods=['POST'])
@login_required
def execute_data_completion():
    """执行补全急救：调用 TMDB 接口自动清洗片名、搜索并挂载 ID、下载缺失的海报（带完整日志文件记录）"""
    payload = request.json
    if not payload or not isinstance(payload, list):
        return jsonify({"status": "error", "message": "未收到任何补全任务"})

    def generate():
        logger.info(f"[数据补全] 补全急救任务开始！共收到 {len(payload)} 个处理项")
        yield f"data: {json.dumps({'status': 'syncing', 'name': '正在初始化急救引擎...'})}\n\n"

        # 代理与 Session 初始化
        raw_proxies = get_user_proxies(current_user)
        safe_proxies = None
        if raw_proxies:
            safe_proxies = {}
            for k, v in raw_proxies.items():
                if v:
                    clean_url = str(v).strip().lower().replace("https://", "").replace("http://", "")
                    safe_proxies[k] = f"http://{clean_url}"

        session = requests.Session()
        session.trust_env = False
        session.headers.update({'Connection': 'close'})
        if safe_proxies:
            session.proxies.update(safe_proxies)

        retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        session.mount('https://', HTTPAdapter(max_retries=retries, pool_connections=15, pool_maxsize=15))

        def download_tmdb_image(url_path, folder="posters", max_retries=3):
            if not url_path: return "images/logo.png"
            filename = url_path.strip("/")
            save_dir = os.path.join(app.root_path, 'static', 'images', folder)
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, filename)
            relative_path = f"images/{folder}/{filename}"
            if os.path.exists(save_path): return relative_path

            img_url = (
                          "https://image.tmdb.org/t/p/w1280" if folder == "backdrops" else "https://image.tmdb.org/t/p/w500") + url_path
            for _ in range(max_retries):
                try:
                    resp = session.get(img_url, timeout=(3.05, 30))
                    if resp.status_code == 200:
                        with open(save_path, 'wb') as f:
                            f.write(resp.content)
                        return relative_path
                except:
                    pass
                time.sleep(1.5)
            return "images/logo.png"

        total_items = len(payload)
        for idx, item in enumerate(payload, 1):
            poster_id = item.get('id')
            raw_title = item.get('title')
            m_type = 'movie' if item.get('raw_type') == 'Movie' else 'tv'

            # ✨ 增强修复：去除末尾的括号，并强制抹除首尾的破折号、下划线、波浪号等干扰符号
            clean_title = re.sub(r'\s*\(.*?\)$', '', raw_title).strip('- _~= ')

            # 尝试提取年份供 TMDB 电影搜索做高精度匹配
            year_match = re.search(r'\((\d{4})\)', raw_title)
            release_year = year_match.group(1) if year_match else None

            logger.info(f"[数据补全] [{idx}/{total_items}] 正在处理: {raw_title}")
            logger.info(f"[数据补全] 提取纯净搜索词: '{clean_title}', 识别年份: {release_year}")

            yield f"data: {json.dumps({'status': 'syncing', 'name': f'({idx}/{total_items}) 检索: {clean_title}'})}\n\n"

            try:
                poster = WatchPoster.query.filter_by(id=poster_id, user_id=current_user.id).first()
                if not poster:
                    logger.warning(f"[数据补全] 数据库未找到 ID={poster_id} 的记录，跳过。")
                    continue

                target_tmdb_id = poster.tmdb_id

                # 1. 缺失 TMDB ID，拿着纯净搜索词去 TMDB 找回真身
                if not target_tmdb_id:
                    search_url = f"https://api.themoviedb.org/3/search/{m_type}"
                    params = {
                        "api_key": current_user.tmdb_api_key,
                        "query": clean_title,
                        "language": "zh-CN"
                    }
                    if release_year and m_type == 'movie':
                        params['primary_release_year'] = release_year

                    resp = session.get(search_url, params=params, timeout=10)
                    if resp.status_code == 200:
                        results = resp.json().get('results', [])
                        if results:
                            target_tmdb_id = str(results[0].get('id'))
                            poster.tmdb_id = target_tmdb_id
                            logger.info(f"[数据补全] 搜索命中！找回遗失的 TMDB ID: {target_tmdb_id}")
                        else:
                            logger.warning(f"[数据补全] 搜索扑空：TMDB 未收录词条 '{clean_title}'")
                    else:
                        logger.error(f"[数据补全] 搜索请求失败！状态码: {resp.status_code}")

                # 2. 有了 ID 后，顺藤摸瓜把海报和详情都拉回来
                if target_tmdb_id:
                    meta_url = f"https://api.themoviedb.org/3/{m_type}/{target_tmdb_id}"
                    meta_resp = session.get(meta_url,
                                            params={"api_key": current_user.tmdb_api_key, "language": "zh-CN"},
                                            timeout=10)

                    if meta_resp.status_code == 200:
                        meta = meta_resp.json()
                        if poster.media_type == "Movie":
                            poster.display_title = meta.get('title', poster.display_title)
                        else:
                            poster.display_title = meta.get('name', poster.display_title)
                            poster.series_name = poster.display_title

                        poster.overview = meta.get('overview', poster.overview)
                        poster.season_overview = poster.overview

                        new_poster = download_tmdb_image(meta.get('poster_path'), "posters")
                        new_backdrop = download_tmdb_image(meta.get('backdrop_path'), "backdrops")

                        if new_poster != "images/logo.png":
                            poster.local_image_path = new_poster
                            poster.series_image_path = new_poster
                        if new_backdrop != "images/logo.png":
                            poster.backdrop_image_path = new_backdrop
                            poster.background_image_path = new_backdrop

                        # 同步连带把底层的 WatchRecord 也补齐
                        sync_records = WatchRecord.query.filter_by(user_id=current_user.id, title=raw_title).all()
                        for r in sync_records:
                            r.tmdb_id = target_tmdb_id

                        db.session.commit()
                        logger.info(f"[数据补全] {clean_title} 元数据与海报全部拼图补齐！")
                    else:
                        logger.error(f"[数据补全] TMDB 详情获取失败，状态码: {meta_resp.status_code}")
                else:
                    logger.warning(f"[数据补全] 缺失基础身份标识，放弃海报补全。")

            except Exception as e:
                db.session.rollback()
                logger.error(f"[数据补全] 处理时发生异常崩断: {str(e)}")

            # TMDB 频率安全锁
            time.sleep(0.5)

        session.close()
        logger.info("[数据补全] 全部数据修补任务已经执行完毕！")
        yield f"data: {json.dumps({'status': 'done'})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')





@app.route('/api/execute_watcharr_import', methods=['POST'])
@login_required
def execute_watcharr_import():
    """极致精简前端推送，但后台保持全面下载、中文化、精准时间继承与专业级详尽日志记录"""
    payload = request.json
    if not payload:
        logger.warning("[Watcharr导入] 收到空数据，导入任务中止。")
        return jsonify({"status": "error", "message": "未收到任何勾选数据"})

    def generate():
        logger.info("[Watcharr导入] 任务开始，正在初始化导入引擎...")
        yield f"data: {json.dumps({'status': 'syncing', 'name': '正在初始化导入引擎...'})}\n\n"

        # ========================================================
        # 局部代理净化区
        # ========================================================
        raw_proxies = get_user_proxies(current_user)
        safe_proxies = None

        if raw_proxies:
            safe_proxies = {}
            for k, v in raw_proxies.items():
                if v:
                    clean_url = str(v).strip().lower().replace("https://", "").replace("http://", "")
                    safe_proxies[k] = f"http://{clean_url}"
            logger.info(f"[Watcharr导入] 代理已映射为安全 HTTP 通道: {safe_proxies}")
        else:
            logger.info("[Watcharr导入] 未配置代理，使用直连。")

        session = requests.Session()
        session.trust_env = False
        session.headers.update({'Connection': 'close'})

        if safe_proxies:
            session.proxies.update(safe_proxies)

        retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        session.mount('https://', HTTPAdapter(max_retries=retries, pool_connections=15, pool_maxsize=15))
        logger.info("[Watcharr导入] HTTP 连接池与重试机制初始化完成。")

        def download_tmdb_image(url_path, folder="posters", max_retries=3):
            if not url_path:
                logger.debug(f"[图片下载] 缺少图片 URL 路径 (folder={folder})，使用占位图。")
                return "images/logo.png"

            # 1. 提取 TMDB 原始文件名 (例如: ysSHtaqOwYvW9JUH8VJS1XVHOh5.jpg)
            filename = url_path.strip("/")

            # 2. 提前构建本地存储路径
            save_dir = os.path.join(app.root_path, 'static', 'images', folder)
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, filename)
            relative_path = f"images/{folder}/{filename}"

            # 3. 核心防重逻辑：如果本地已经存在这个文件，直接返回路径，瞬间跳过下载
            if os.path.exists(save_path):
                logger.debug(f"[图片下载] 图片已存在本地，跳过: {filename}")
                return relative_path

            base_url = "https://image.tmdb.org/t/p/w1280" if folder == "backdrops" else "https://image.tmdb.org/t/p/w500"
            img_url = base_url + url_path
            logger.debug(f"[图片下载] 开始下载图片 (folder={folder}): {img_url}")

            for attempt in range(max_retries):
                try:
                    resp = session.get(img_url, timeout=(3.05, 30))

                    if resp.status_code == 200:
                        with open(save_path, 'wb') as f:
                            f.write(resp.content)
                        logger.debug(f"[图片下载] 下载成功: {save_path}")
                        return relative_path
                    else:
                        logger.warning(f"[图片下载] HTTP 状态码 {resp.status_code}，准备重试: {img_url}")

                except requests.exceptions.ReadTimeout:
                    logger.warning(f"[图片下载] 第 {attempt + 1}/{max_retries} 次尝试读取超时: {img_url}")
                except requests.exceptions.ConnectionError:
                    logger.warning(
                        f"[图片下载] 第 {attempt + 1}/{max_retries} 次尝试连接失败: {img_url}")
                except Exception as e:
                    logger.error(f"[图片下载] 网络异常（致命）: {img_url}: {e}")
                    break

                time.sleep(1.5)

            logger.error(f"[图片下载] 重试 {max_retries} 次仍失败，放弃下载并使用占位图: {img_url}")
            return "images/logo.png"

        def fetch_tmdb_metadata(t_id, m_type):
            url = f"https://api.themoviedb.org/3/{m_type}/{t_id}"
            logger.debug(f"[TMDB接口] 获取元数据: {url}")
            try:
                resp = session.get(url, params={"api_key": current_user.tmdb_api_key, "language": "zh-CN"}, timeout=10)
                if resp.status_code == 200:
                    logger.debug(f"[TMDB接口] 成功获取元数据: ID={t_id}")
                    return resp.json()
                else:
                    logger.warning(f"[TMDB接口] 获取元数据失败，HTTP 状态码: {resp.status_code}")
            except Exception as e:
                logger.error(f"[TMDB接口] 获取元数据时网络异常: {e}")
            return {}

        def fetch_tmdb_season(t_id, s_num):
            url = f"https://api.themoviedb.org/3/tv/{t_id}/season/{s_num}"
            logger.debug(f"[TMDB接口] 获取季元数据: {url}")
            try:
                resp = session.get(url, params={"api_key": current_user.tmdb_api_key, "language": "zh-CN"}, timeout=10)
                if resp.status_code == 200:
                    logger.debug(f"[TMDB接口] 成功获取季元数据: ID={t_id}, 季={s_num}")
                    return resp.json()
                else:
                    logger.warning(f"[TMDB接口] 获取季元数据失败，HTTP 状态码: {resp.status_code}")
            except Exception as e:
                logger.error(f"[TMDB接口] 获取季元数据时网络异常: {e}")
            return {}

        tmdb_season_cache = {}
        total_items = len(payload)
        logger.info(f"[Watcharr导入] 共解析到 {total_items} 个待处理条目")
        # 批量提交阈值：每处理 COMMIT_BATCH_SIZE 个条目提交一次事务，减少 SQLite 提交开销
        COMMIT_BATCH_SIZE = 25
        batch_count = 0

        try:
            for idx, item in enumerate(payload, 1):
                core_id = item.get('core_id')
                tmdb_id = item.get('tmdb_id')

                if str(tmdb_id).lower() in ['none', 'null', '']:
                    tmdb_id = None

                item_type = item.get('type')
                fallback_title = item.get('title')
                records = item.get('records', [])

                logger.info(f"[Watcharr导入] ------------- 正在处理第 {idx}/{total_items} 项 -------------")
                logger.info(f"[Watcharr导入] 原始标题: {fallback_title} | 类型: {item_type} | TMDB_ID: {tmdb_id}")

                tmdb_title = fallback_title
                overview = ""
                local_poster = "images/logo.png"
                local_backdrop = None
                meta = None

                # 1. 优先获取中文元数据
                if tmdb_id:
                    meta = fetch_tmdb_metadata(tmdb_id, 'movie' if item_type == 'movie' else 'tv')
                    if meta:
                        tmdb_title = meta.get('title') if item_type == 'movie' else meta.get('name', fallback_title)
                        overview = meta.get('overview', '')
                        logger.info(f"[Watcharr导入] 标题已本地化: {tmdb_title}")
                    else:
                        logger.warning(f"[Watcharr导入] 元数据获取失败，回退使用原标题: {fallback_title}")

                # 2. 推送已经中文化的标题给前端（在耗时的图片下载前推送，防止UI假死）
                yield f"data: {json.dumps({'status': 'syncing', 'name': f'({idx}/{total_items}) {tmdb_title}'})}\n\n"

                # 3. 推送完成后，后台安心执行耗时的图片下载逻辑
                if meta:
                    local_poster = download_tmdb_image(meta.get('poster_path'), "posters")
                    local_backdrop = download_tmdb_image(meta.get('backdrop_path'), "backdrops")

                episodes_to_insert = []
                if item_type == 'tv':
                    for r in records:
                        s_num = r['season']
                        rec_date = datetime.fromisoformat(r['watch_date']) if r.get('watch_date') else datetime.now()

                        specific_times_raw = r.get('specific_times', {})
                        specific_times = {}
                        for e_str, t_str in specific_times_raw.items():
                            try:
                                specific_times[int(e_str)] = datetime.fromisoformat(t_str)
                            except:
                                pass

                        cache_key = f"{tmdb_id}_s{s_num}"
                        if cache_key not in tmdb_season_cache:
                            tmdb_season_cache[cache_key] = fetch_tmdb_season(tmdb_id, s_num) if tmdb_id else {}

                        season_meta = tmdb_season_cache[cache_key]
                        ep_meta_list = season_meta.get('episodes', [])

                        ep_list = [ep.get('episode_number') for ep in ep_meta_list] if r['episode'] == '全季' and ep_meta_list else [int(r['episode']) if r['episode'] != '全季' else 1]

                        episodes_to_insert.append({
                            's_num': s_num,
                            'ep_list': ep_list,
                            'date_played': rec_date,
                            'specific_times': specific_times,
                            'season_meta': season_meta
                        })
                        logger.info(f"[Watcharr导入] 已处理第 {s_num} 季，解析单集映射: {ep_list}")

                try:
                    logger.info(f"[数据库] 正在获取数据库锁: {tmdb_title}")
                    with db_lock:
                        poster_target = tmdb_id if (item_type == 'movie' and tmdb_id) else (core_id if item_type == 'movie' else f"{core_id}_S1")
                        poster = WatchPoster.query.filter_by(user_id=current_user.id, target_id=poster_target).first()

                        if not poster:
                            logger.info(f"[数据库] 新建海报记录，目标 ID: {poster_target}")
                            db.session.add(WatchPoster(
                                user_id=current_user.id, target_id=poster_target,
                                media_type="Movie" if item_type == 'movie' else "Series",
                                display_title=tmdb_title, series_name=None if item_type == 'movie' else tmdb_title,
                                season_num=None if item_type == 'movie' else records[0]['season'],
                                local_image_path=local_poster, series_image_path=local_poster,
                                backdrop_image_path=local_backdrop, background_image_path=local_backdrop,
                                overview=overview, season_overview=overview,
                                last_watched_date=datetime.now(), tmdb_id=tmdb_id or None, is_deleted=False
                            ))
                        else:
                            logger.info(f"[数据库] 更新已有海报记录，目标 ID: {poster_target}")
                            poster.display_title, poster.overview, poster.local_image_path, poster.is_deleted = tmdb_title, overview, local_poster, False
                            if local_backdrop: poster.backdrop_image_path = local_backdrop

                        if item_type == 'movie':
                            rec_date = datetime.fromisoformat(records[0]['watch_date']) if records and records[0].get('watch_date') else datetime.now()
                            rec_id = str(tmdb_id) if tmdb_id else f"watcharr_{core_id}"
                            rec = WatchRecord.query.filter_by(user_id=current_user.id, item_id=rec_id).first()
                            if not rec:
                                logger.info(f"[数据库] 新建电影观看记录，记录 ID: {rec_id}")
                                db.session.add(WatchRecord(
                                    user_id=current_user.id, item_id=rec_id, item_type="Movie",
                                    library_name="Watcharr导入",
                                    title=tmdb_title, source="watcharr", tmdb_id=tmdb_id or None, date_played=rec_date,
                                    is_deleted=False
                                ))
                            else:
                                logger.info(f"[数据库] 更新已有电影观看记录，记录 ID: {rec_id}")
                                rec.date_played, rec.is_deleted = rec_date, False

                        elif item_type == 'tv':
                            for block in episodes_to_insert:
                                s_num, ep_list, fallback_date = block['s_num'], block['ep_list'], block['date_played']
                                specific_times = block.get('specific_times', {})
                                ep_meta_map = {ep.get('episode_number'): ep for ep in block['season_meta'].get('episodes', [])}

                                for e_num in ep_list:
                                    actual_date = specific_times.get(e_num, fallback_date)

                                    rec_id = f"{tmdb_id}_{s_num}_{e_num}" if tmdb_id else f"watcharr_{core_id}_{s_num}_{e_num}"
                                    ep_data = ep_meta_map.get(e_num, {})
                                    zh_ep_name = ep_data.get('name', f"第 {e_num} 集")
                                    zh_ep_overview = ep_data.get('overview', '')

                                    rec = WatchRecord.query.filter_by(user_id=current_user.id, item_id=rec_id).first()
                                    if not rec:
                                        logger.info(f"[数据库] 新建剧集观看记录 (第 {e_num} 集)，时间: {actual_date}")
                                        db.session.add(WatchRecord(
                                            user_id=current_user.id, item_id=rec_id, item_type="Episode",
                                            library_name="Watcharr导入",
                                            title=f"第 {e_num} 集 - {zh_ep_name}", series_name=tmdb_title,
                                            season_name=f"第 {s_num} 季",
                                            episode_num=e_num, source="watcharr", tmdb_id=tmdb_id or None,
                                            date_played=actual_date, is_deleted=False
                                        ))
                                    else:
                                        logger.info(f"[数据库] 更新已有剧集观看记录 (第 {e_num} 集)，时间: {actual_date}")
                                        rec.date_played, rec.is_deleted = actual_date, False

                                    detail = EpisodeDetail.query.filter_by(item_id=rec_id).first()
                                    if not detail:
                                        logger.info(f"[数据库] 处理单集详情并下载剧照: {rec_id}")
                                        local_still = download_tmdb_image(ep_data.get('still_path'), "stills")
                                        db.session.add(EpisodeDetail(
                                            item_id=rec_id, series_name=tmdb_title, season_num=s_num, episode_num=e_num,
                                            episode_name=zh_ep_name, overview=zh_ep_overview,
                                            series_tmdb_id=tmdb_id or None,
                                            still_image_path=local_still
                                        ))
                    batch_count += 1
                    if batch_count >= COMMIT_BATCH_SIZE:
                        db.session.commit()
                        logger.info(f"[数据库] 批量提交成功：第 {idx // COMMIT_BATCH_SIZE} 批，共 {batch_count} 条")
                        batch_count = 0
                except Exception as db_err:
                    db.session.rollback()
                    batch_count = 0
                    logger.error(f"[数据库] 处理条目 '{fallback_title}' 时异常，已回滚: {str(db_err)}")

            # 收尾：提交剩余不足一批的条目
            if batch_count > 0:
                db.session.commit()
                logger.info(f"[数据库] 批量提交成功：收尾批次，共 {batch_count} 条")

        except GeneratorExit:
            logger.info("[Watcharr导入] 客户端主动中止了导入任务，后台进程已安全结束。")
            session.close()
            return
        except Exception as e:
            db.session.rollback()
            logger.error(f"[Watcharr导入] 主循环出现未预期异常: {e}")

        session.close()
        logger.info("[Watcharr导入] 所有导入任务执行完毕。")
        yield f"data: {json.dumps({'status': 'done'})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


# ==========================================
# 日志管理面板路由及文件写入逻辑
# ==========================================

def log_print(msg):
    """一个全局的小工具函数，可以同时往控制台和文件里打印日志信息。"""
    print(msg)

    log_dir = os.path.join(app.root_path, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, 'jellywall.log')

    with open(log_file_path, 'a', encoding='utf-8') as f:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        f.write(f"[{timestamp}] {msg}\n")


@app.route('/logs')
@login_required
def logs_view():
    """渲染前台的网页版日志查看控制台。"""
    return render_template('logs.html', title="日志管理")


@app.route('/api/log_stream')
@login_required
def log_stream():
    """用来把后台的日志实时推给前端 SSE 连接展示的流接口，还带了往上回溯一百行的功能。"""
    log_dir = os.path.join(app.root_path, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, 'jellywall.log')

    if not os.path.exists(log_file_path):
        open(log_file_path, 'a', encoding='utf-8').close()

    def generate():
        # 优化:不再 readlines() 全量读文件,只从文件尾部读最近 1MB
        with open(log_file_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            read_size = min(file_size, 1024 * 1024)
            f.seek(-read_size, os.SEEK_END)
            chunk = f.read().decode('utf-8', errors='replace')
            tail_lines = chunk.splitlines()
            if file_size > read_size:
                # 读取窗口头部可能截断半行,丢弃第一行保证完整性
                tail_lines = tail_lines[1:]

            indexed_lines = []
            biz_count = 0
            sys_count = 0

            for i in range(len(tail_lines) - 1, -1, -1):
                line = tail_lines[i]
                if not line.strip():
                    continue

                if '[System-HTTP]' not in line:
                    if biz_count < 100:
                        indexed_lines.append((i, line))
                        biz_count += 1
                else:
                    if sys_count < 100:
                        indexed_lines.append((i, line))
                        sys_count += 1

                if biz_count >= 100 and sys_count >= 100:
                    break

            indexed_lines.sort(key=lambda x: x[0])

            for _, line in indexed_lines:
                yield f"data: {line.strip()}\n\n"

            while True:
                raw_line = f.readline()
                if not raw_line:
                    time.sleep(0.5)
                    f.seek(0, 1)
                    continue
                line = raw_line.decode('utf-8', errors='replace').rstrip('\r\n')

                if line.strip():
                    yield f"data: {line.strip()}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/demo')
@login_required
def demo_preview():
    """渲染出一个静态做样子的预览详情页面。"""
    return render_template('demo_detail.html', title="详情页预览")


def get_system_config():
    """读取这套系统的基础开关配置，找不到就默认开启允许新用户注册。"""
    config_path = os.path.join(app.root_path, 'config', 'system_config.json')
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"allow_registration": True}


def save_system_config(config_data):
    """保存这套系统的基础开关配置。"""
    config_dir = os.path.join(app.root_path, 'config')
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, 'system_config.json')
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, indent=4)




if __name__ == '__main__':
    # 调试模式由环境变量控制：FLASK_DEBUG=1 开启（开发用），生产环境默认关闭
    app.debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes', 'on')
    if app.debug:
        logger.info("[启动] 调试模式已开启 (FLASK_DEBUG=1)")
    else:
        logger.info("[启动] 生产模式：调试器已关闭 (可设置 FLASK_DEBUG=1 开启)")

    # 项目启动时保证数据库表结构都顺利建好
    with app.app_context():
        db.create_all()

        # 存量数据库补业务索引(幂等,新库由模型自动创建,旧库走 IF NOT EXISTS)
        index_sqls = [
            "CREATE INDEX IF NOT EXISTS ix_watch_record_user_type ON watch_record (user_id, is_deleted, item_type)",
            "CREATE INDEX IF NOT EXISTS ix_watch_record_user_series ON watch_record (user_id, series_name, is_deleted)",
            "CREATE INDEX IF NOT EXISTS ix_watch_record_user_date ON watch_record (user_id, date_played)",
            "CREATE INDEX IF NOT EXISTS ix_watch_poster_user_type ON watch_poster (user_id, media_type, is_deleted)",
            "CREATE INDEX IF NOT EXISTS ix_watch_poster_user_series ON watch_poster (user_id, series_name, is_deleted)",
            "CREATE INDEX IF NOT EXISTS ix_episode_detail_series ON episode_detail (series_tmdb_id, season_num)",
        ]
        for sql in index_sqls:
            db.session.execute(text(sql))
        db.session.commit()

    import os

    # 做个判断，环境是 Werkzeug 的真正工作子进程，或者没有开调试才启动调度器
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
        refresh_scheduler_jobs()
        scheduler.start()

    run_port = 5000

    # 启动前看看本地的配置文件里有没有专门去修改过端口，有的话就用新端口启动
    try:
        config_path = os.path.join(app.root_path, 'config', 'users.json')
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                users_data = json.load(f)
                for u in users_data.values():
                    if u.get('web_port'):
                        run_port = int(u['web_port'])
                        break
    except Exception as e:
        logger.warning(f"[启动] 读取自定义端口失败，将使用默认端口 5000。原因: {e}")

    logger.info(f"[启动] JellyWall v{APP_VERSION} 即将启动，运行端口: {run_port}")

    app.run(host='0.0.0.0', port=run_port)
