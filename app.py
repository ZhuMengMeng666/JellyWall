from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify,stream_with_context
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash  # 确保顶部引入了加密库
import threading
import uuid  # 确保文件顶部有引入此模块，用于生成用户的独立 ID
from werkzeug.security import check_password_hash
from flask_login import login_user
import re
import json
import sys  # ✨ 确保引入 sys 模块
import requests
from datetime import datetime
from flask import request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import time
from flask import stream_with_context, Response



# ====== ✨ 新增：TMDB 搜索短时缓存池 (存放格式: {query: {timestamp: float, data: list}}) ======
TMDB_SEARCH_CACHE = {}
TMDB_DETAIL_CACHE = {}
TMDB_TV_EP_COUNT_CACHE = {}  # ✨ 新增：专门缓存剧集的官方总集数
CACHE_TTL = 3600
# ====== ✨ 新增：全局数据库锁，防止多线程把 SQLite 写死机 ======
db_lock = threading.Lock()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'jellywall_super_secret_key_2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///project.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

import logging
import os
import re
import sys

# ... (你的其他 app 代码) ...

# ==========================================
# 🌟 核心：接管日志引擎 (区分系统 HTTP 与 业务日志)
# ==========================================
log_dir = os.path.join(app.root_path, 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file_path = os.path.join(log_dir, 'jellywall.log')


# 自定义智能日志格式化器
class SmartFileFormatter(logging.Formatter):
    def format(self, record):
        # 统一获取标准时间
        record.asctime = self.formatTime(record, self.datefmt)

        # 判断日志来源
        if record.name == 'werkzeug':
            # 1. 如果是底层 HTTP 请求日志：清洗原生时间戳，加上 [System-HTTP] 前缀
            clean_msg = re.sub(r'\[\d{2}/[A-Za-z]{3}/\d{4} \d{2}:\d{2}:\d{2}\]\s*', '', record.getMessage())
            log_str = f"[System-HTTP] [{record.asctime}] {clean_msg}"
        else:
            # 2. 如果是咱们自己的业务日志：加上 [INFO], [WARNING], [ERROR] 等级别前缀
            log_str = f"[{record.levelname}] [{record.asctime}] {record.getMessage()}"

        # 全局统一剔除控制台颜色乱码，保证写入文件的纯净
        log_str = re.sub(r'\x1b\[[0-9;]*m', '', log_str)
        return log_str


# 1. 文件处理器 (输出给前端看的，挂载智能清洗器)
file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
formatter = SmartFileFormatter(datefmt='%Y-%m-%d %H:%M:%S')
file_handler.setFormatter(formatter)

# 2. 控制台处理器 (输出给 PyCharm 看的)
console_handler = logging.StreamHandler(sys.stdout)
# 为控制台也加一个基础的格式化器，这样你在 PyCharm 里也能清晰看到日志级别
console_formatter = logging.Formatter('[%(levelname)s] %(message)s')
console_handler.setFormatter(console_formatter)

# 3. 拦截 Werkzeug 日志器
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.INFO)
werkzeug_logger.handlers.clear()
werkzeug_logger.addHandler(file_handler)
werkzeug_logger.addHandler(console_handler)

# (可选) 接管 Flask 自身日志
app.logger.handlers.clear()
app.logger.addHandler(file_handler)
app.logger.addHandler(console_handler)
app.logger.setLevel(logging.INFO)

# ====== 初始化业务专属 Logger ======
logger = logging.getLogger('jellywall')
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ====== ✨ 修改点：将 JSON 路径指向 config 文件夹 ======
CONFIG_DIR = os.path.join(app.root_path, 'config')
# 自动检测并创建 config 文件夹（如果不存在的话）
os.makedirs(CONFIG_DIR, exist_ok=True)

# 指定 user.json 的完整路径
USERS_FILE = os.path.join(CONFIG_DIR, 'users.json')

def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(users, f, ensure_ascii=False, indent=4)

# ====== ✨ 重写：纯 Python 类的 User 模型 ======
class User(UserMixin):
    def __init__(self, id, username, password, jellyfin_url=None, jellyfin_api_key=None, jellyfin_user_id=None, proxy_url=None, proxy_port=None, tmdb_api_key=None, web_port=None, sync_enabled=False, sync_cron="0 * * * *"):
        self.id = str(id)  # JSON 的 key 必须是字符串
        self.username = username
        self.password = password
        self.jellyfin_url = jellyfin_url
        self.jellyfin_api_key = jellyfin_api_key
        self.jellyfin_user_id = jellyfin_user_id
        self.proxy_url = proxy_url
        self.proxy_port = proxy_port
        self.tmdb_api_key = tmdb_api_key
        self.web_port = web_port  # ✨ 赋值给对象属性
        self.sync_enabled = sync_enabled
        self.sync_cron = sync_cron

    # 将对象转为字典，方便存入 JSON
    def to_dict(self):
        return {
            "id": self.id, "username": self.username, "password": self.password,
            "jellyfin_url": self.jellyfin_url, "jellyfin_api_key": self.jellyfin_api_key,
            "jellyfin_user_id": self.jellyfin_user_id, "proxy_url": self.proxy_url,
            "proxy_port": self.proxy_port,
            "tmdb_api_key": self.tmdb_api_key,
            "web_port": self.web_port,  # ✨ 保存到 JSON 时带上这个字段
            "sync_enabled": self.sync_enabled,  # ✨ 保存到 JSON
            "sync_cron": self.sync_cron  # ✨ 保存到 JSON
        }

    # 自带的保存方法，随时更新自己的信息到 JSON
    def save(self):
        users = load_users()
        users[self.id] = self.to_dict()
        save_users(users)

class WatchRecord(db.Model):
    """本地观影记录明细表"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), nullable=False)
    item_id = db.Column(db.String(100), nullable=False)  # Jellyfin 里的 ID 或其他平台生成的唯一ID
    item_type = db.Column(db.String(50), nullable=False)  # 'Movie' 或 'Episode'
    library_name = db.Column(db.String(100), nullable=False)

    title = db.Column(db.String(200), nullable=False)
    series_name = db.Column(db.String(200))
    season_name = db.Column(db.String(100))

    # ====== ✨ 新增字段 ======
    episode_num = db.Column(db.Integer, nullable=True)  # 💥 专门记录具体是第几集的数字，方便后续排序和统计
    source = db.Column(db.String(50), nullable=False, default='Jellyfin') # 💥 记录历史来源：Jellyfin, tmdb, watcharr
    tmdb_id = db.Column(db.String(50), nullable=True)
    date_played = db.Column(db.DateTime, nullable=False)
    # ====== ✨ 新增：软删除标记 ======
    is_deleted = db.Column(db.Boolean, default=False)

    __table_args__ = (db.UniqueConstraint('user_id', 'item_id', name='_user_item_uc'),)


class WatchPoster(db.Model):
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

    # ====== ✨ 剧集与季度的双重简介 ======
    overview = db.Column(db.Text, nullable=True)
    season_overview = db.Column(db.Text, nullable=True)

    last_watched_date = db.Column(db.DateTime, nullable=False)
    # ====== ✨ 新增：软删除标记 ======
    is_deleted = db.Column(db.Boolean, default=False)
    __table_args__ = (db.UniqueConstraint('user_id', 'target_id', 'display_title', name='_user_poster_uc'),)


class EpisodeDetail(db.Model):
    """单集元数据与剧照缓存表"""
    id = db.Column(db.Integer, primary_key=True)

    item_id = db.Column(db.String(100), unique=True, nullable=False)  # Jellyfin/TMDB 的单集ID

    series_name = db.Column(db.String(200))  # 所属剧集名
    season_num = db.Column(db.Integer)  # 第几季
    episode_num = db.Column(db.Integer)  # 第几集

    episode_name = db.Column(db.String(200))  # 单集专属名称
    overview = db.Column(db.Text)  # 剧情内容介绍
    series_tmdb_id = db.Column(db.String(50), nullable=True)
    still_image_path = db.Column(db.String(255))  # 本地单集剧照路径


def update_episode_detail(item, jf_url, headers, still_dir, series_tmdb_id):
    item_id = item["Id"]
    existing_detail = EpisodeDetail.query.filter_by(item_id=item_id).first()
    if existing_detail: return

    series_name = item.get("SeriesName", "未知剧集")
    episode_name = item.get("Name", "未知集名")

    # ✨ 修复 null 问题：用 or "" 强制转换 None 为空字符串
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
    if not download_image(img_url, headers, still_path):
        still_relative_path = "images/logo.png"

    new_detail = EpisodeDetail(
        item_id=item_id, series_name=series_name, season_num=season_num, episode_num=episode_num,
        episode_name=episode_name, overview=overview, still_image_path=still_relative_path,
        series_tmdb_id=series_tmdb_id
    )
    db.session.add(new_detail)


# ==========================================
# 🤖 后台自动化同步引擎 (APScheduler)
# ==========================================
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def background_sync_task(user_id):
    """脱离 Request 上下文的纯后台同步任务"""
    # 必须手动推入 Flask 上下文才能操作数据库
    with app.app_context():
        user = load_user(user_id)
        if not user or not user.jellyfin_url or not user.jellyfin_api_key:
            return

        logger.info(f"[Auto-Sync] 开始为用户 {user.username} 执行定时同步...")
        jf_url = user.jellyfin_url
        headers = {"X-Emby-Token": user.jellyfin_api_key}
        base_user_url = f"{jf_url}/Users/{user.jellyfin_user_id}"

        poster_dir = os.path.join(app.root_path, 'static', 'posters')
        still_dir = os.path.join(app.root_path, 'static', 'stills')
        backdrop_dir = os.path.join(app.root_path, 'static', 'backdrops')
        tmdb_search_cache = {}
        synced_names = set()
        # ✨ 新增：初始化去重集合
        processed_ids = set()

        try:
            views_resp = requests.get(f"{base_user_url}/Views", headers=headers, timeout=10)
            if views_resp.status_code != 200: return

            for view in views_resp.json().get("Items", []):
                items_resp = requests.get(
                    f"{base_user_url}/Items", headers=headers,
                    params={"ParentId": view["Id"], "Filters": "IsPlayed", "IncludeItemTypes": "Movie,Episode",
                            "Recursive": "true", "Limit": 2000,
                            "Fields": "UserData,SeriesName,SeriesId,SeasonId,ParentIndexNumber,Overview,ProviderIds,SeriesProviderIds,SeasonName"},
                    timeout=15
                )
                if items_resp.status_code != 200: continue

                for item in items_resp.json().get("Items", []):
                    # ====== ✨ 新增防重逻辑 ======
                    item_id = item["Id"]
                    if item_id in processed_ids:
                        continue
                    processed_ids.add(item_id)
                    # ==============================
                    dt_local = parse_jellyfin_date(item.get("UserData", {}).get("LastPlayedDate"))
                    if not dt_local: continue

                    master_tmdb_id = get_tmdb_id_smart(user, item, item["Type"], tmdb_search_cache)

                    # ✨ 依然使用你之前设计的全局锁保护 SQLite
                    with db_lock:
                        if update_watch_record(user.id, item, item["Type"], view["Name"], dt_local, master_tmdb_id):
                            update_watch_poster(user.id, user.jellyfin_user_id, item, item["Type"], dt_local,
                                                jf_url, headers, poster_dir, backdrop_dir, synced_names, master_tmdb_id)

                        if item["Type"] == "Episode":
                            update_episode_detail(item, jf_url, headers, still_dir, master_tmdb_id)

            with db_lock:
                db.session.commit()
            logger.info(f"[Auto-Sync] 用户 {user.username} 定时同步完成！")

        except Exception as e:
            logger.error(f"[Auto-Sync] 定时同步失败: {e}")


def refresh_scheduler_jobs():
    """读取所有用户的配置，动态刷新定时任务"""
    scheduler.remove_all_jobs()
    users = load_users()
    for uid, udata in users.items():
        if udata.get('sync_enabled') and udata.get('sync_cron'):
            try:
                # 解析前端传来的 5 位标准 Cron (分 时 日 月 周)
                trigger = CronTrigger.from_crontab(udata['sync_cron'])
                scheduler.add_job(
                    background_sync_task,
                    trigger=trigger,
                    args=[uid],
                    id=f"auto_sync_{uid}",
                    replace_existing=True
                )
                logger.info(f"已挂载用户 {udata.get('username')} 的定时任务: {udata['sync_cron']}")
            except ValueError:
                logger.warning(f"用户 {udata.get('username')} 的 Cron 表达式无效: {udata['sync_cron']}")

@login_manager.user_loader
def load_user(user_id):
    users = load_users()
    if str(user_id) in users:
        return User(**users[str(user_id)])
    return None


# ================= 路由逻辑 =================

@app.route('/')
def index():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    if not current_user.jellyfin_url or not current_user.jellyfin_api_key:
        return redirect(url_for('onboarding'))
    return redirect(url_for('dashboard'))






@app.route('/login', methods=['GET', 'POST'])
def login():
    # 如果已经登录过，直接跳到仪表板
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        # 1. 在本地 JSON 中寻找这个用户
        users = load_users()
        user_data = next((u for u in users.values() if u.get('username') == username), None)

        # 2. 验证用户存在，并且密码正确
        if user_data and check_password_hash(user_data['password'], password):
            # 3. 记录登录状态 (将读取到的字典转换为 User 对象)
            login_user(User(**user_data))
            logger.info(f"用户登录成功: {username}")

            # 👇 这里就是登录成功后跳转的页面！通常是 dashboard（仪表板）
            return redirect(url_for('dashboard'))

        else:
            # 如果账号或密码错误，发送提示信息
            flash('用户名或密码错误，请重试。')

    # 如果是 GET 请求，或者密码验证失败，就重新渲染并停留在登录页
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    # 如果用户已经登录，直接跳回主页
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        jellyfin_url = request.form.get('jellyfin_url')  # 如果你注册时需要填这个
        jellyfin_api_key = request.form.get('jellyfin_api_key')  # 同上

        # 读取 JSON 数据检查用户名是否已存在
        users = load_users()
        if any(u.get('username') == username for u in users.values()):
            flash('该用户名已被注册，请换一个重试。')
            return redirect(url_for('register'))

        # 创建新用户并哈希密码，分配全新生成的唯一 ID
        new_id = str(uuid.uuid4().hex)
        new_user = User(
            id=new_id,
            username=username,
            password=generate_password_hash(password),
            # 如果你的数据库现在不需要绑 Jellyfin，这两行可以删掉，放到配置页再去绑
            jellyfin_url=jellyfin_url,
            jellyfin_api_key=jellyfin_api_key
        )

        # 直接保存至 JSON 文件
        new_user.save()

        logger.info(f"新用户注册成功: {username}")

        flash('注册成功！请登录。')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/onboarding', methods=['GET', 'POST'])
@login_required
def onboarding():
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

                # 触发自身的存入 JSON 方法
                current_user.save()

                return redirect(url_for('dashboard'))
            elif resp.status_code == 401:
                flash('绑定失败：Jellyfin 用户名或密码错误。')
            else:
                flash(f'绑定失败：服务器返回状态码 {resp.status_code}')
        except Exception as e:
            flash(f'无法连接到 Jellyfin，请检查网络或配置。详细: {str(e)}')
            logger.error(f"Jellyfin 绑定连通性测试失败: {str(e)}")

    return render_template('onboarding.html')


@app.route('/dashboard')
@login_required
def dashboard():
    # ====== 1. 电影数据多维统计 ======
    movies_query = WatchRecord.query.filter_by(user_id=current_user.id, item_type='Movie', is_deleted=False)
    movie_total = movies_query.count()
    movie_jf = movies_query.filter_by(source='Jellyfin').count()
    movie_tmdb = movie_total - movie_jf

    # ====== 2. 剧集数据多维统计 ======
    eps_query = WatchRecord.query.filter_by(user_id=current_user.id, item_type='Episode', is_deleted=False)
    ep_total = eps_query.count()
    ep_jf = eps_query.filter_by(source='Jellyfin').count()
    ep_tmdb = ep_total - ep_jf

    # 按剧名去重，计算看了多少“部”剧
    unique_series = db.session.query(WatchRecord.series_name).filter_by(
        user_id=current_user.id, item_type='Episode', is_deleted=False
    ).distinct().count()

    # ====== 3. 最近观看足迹 (带海报与格式化标签) ======
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

    # ====== ✨ 4. 观影热力图数据聚合 ======
    # 计算一年前的时间点，减少数据库扫描压力
    one_year_ago = datetime.now() - timedelta(days=365)
    heatmap_records = WatchRecord.query.filter(
        WatchRecord.user_id == current_user.id,
        WatchRecord.is_deleted == False,
        WatchRecord.date_played >= one_year_ago
    ).all()

    heatmap_data = {}
    for r in heatmap_records:
        # 将 datetime 格式化为纯日期字符串，例如 '2026-06-15'
        date_str = r.date_played.strftime('%Y-%m-%d')

        # 初始化当天的字典结构
        if date_str not in heatmap_data:
            heatmap_data[date_str] = {'movies': 0, 'episodes': 0}

        # 累加当天的观看数量
        if r.item_type == 'Movie':
            heatmap_data[date_str]['movies'] += 1
        else:
            heatmap_data[date_str]['episodes'] += 1

    return render_template('dashboard.html', title="仪表板",
                           movie_total=movie_total, movie_jf=movie_jf, movie_tmdb=movie_tmdb,
                           ep_total=ep_total, ep_jf=ep_jf, ep_tmdb=ep_tmdb,
                           unique_series=unique_series,
                           recent_feed=recent_feed,
                           heatmap_data=heatmap_data)  # ✨ 将处理好的字典喂给前端


@app.route('/test_proxy', methods=['POST'])
@login_required
def test_proxy():
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
        # 尝试通过代理访问一个高可用的地址进行测试
        test_resp = requests.get("http://www.google.com", proxies=proxies, timeout=5)
        if test_resp.status_code == 200:
            return jsonify({"success": True, "message": "✅ 测试代理成功！网络已连通。"})
        else:
            return jsonify({"success": False, "message": f"❌ 测试失败：代理服务器返回状态码 {test_resp.status_code}"})
    except Exception as e:
        return jsonify({"success": False, "message": f"❌ 连接代理失败：{str(e)}"})


@app.route('/test_tmdb', methods=['POST'])
@login_required
def test_tmdb():
    api_key = request.json.get('api_key')
    if not api_key:
        return jsonify({"success": False, "message": "请输入 TMDB API Key"})

    try:
        url = f"https://api.themoviedb.org/3/authentication?api_key={api_key}"

        # ✨ 完美接入已有的全局代理函数
        resp = requests.get(url, proxies=get_user_proxies(current_user), timeout=8)

        if resp.status_code == 200:
            return jsonify({"success": True, "message": "✅ 测试成功！已连通 TMDB。"})
        else:
            return jsonify({"success": False, "message": f"❌ 验证失败：API Key 无效 (状态码 {resp.status_code})"})
    except Exception as e:
        return jsonify({"success": False, "message": f"❌ 连接 TMDB 失败，请检查网络或代理设置：{str(e)}"})


@app.route('/config', methods=['GET', 'POST'])
@login_required
def config():
    if request.method == 'POST':

        # 判断是保存 Jellyfin 配置还是保存代理配置
        form_type = request.form.get('form_type')
        # ====== ✨ 新增：处理自动化同步配置 ======
        if form_type == 'auto_sync_settings':
            sync_enabled = request.form.get('sync_enabled') == 'on'
            sync_cron = request.form.get('sync_cron').strip()

            current_user.sync_enabled = sync_enabled
            current_user.sync_cron = sync_cron
            current_user.save()

            # 配置改变后，立马刷新调度器引擎
            refresh_scheduler_jobs()

            flash("🎉 自动化同步配置已保存生效！")
            return redirect(url_for('config'))
        if form_type == 'proxy_settings':
            current_user.proxy_url = request.form.get('proxy_url').strip()
            current_user.proxy_port = request.form.get('proxy_port').strip()
            # 写入 JSON
            current_user.save()
            flash("🎉 代理配置保存成功！")
            return redirect(url_for('config'))

        # 新增 TMDB 保存逻辑
        if form_type == 'tmdb_settings':
            current_user.tmdb_api_key = request.form.get('tmdb_api_key').strip()
            # 写入 JSON
            current_user.save()
            flash("🎉 TMDB 密钥保存成功！")
            return redirect(url_for('config'))

        # ====== ✨ 新增：处理修改密码请求 ======
        if form_type == 'password_settings':
            old_password = request.form.get('old_password')
            new_password = request.form.get('new_password')
            confirm_password = request.form.get('confirm_password')

            # 1. 验证原密码是否正确
            if not check_password_hash(current_user.password, old_password):
                flash("❌ 原密码错误，请重试。")
            # 2. 验证两次输入的新密码是否一致
            elif new_password != confirm_password:
                flash("❌ 两次输入的新密码不一致。")
                # 3. 验证新密码长度（可选防呆设计）
            elif len(new_password) < 6:
                flash("⚠️ 新密码长度建议不少于 6 位。")
            else:
                # 4. 生成新的哈希密码并保存
                current_user.password = generate_password_hash(new_password)
                logger.info(f"用户 {current_user.username} 成功修改了登录密码。")
                current_user.save()  # 自动写入 config/users.json

                # ====== ✨ 核心改动：修改密码后立即注销当前用户，并踢回登录页 ======
                logout_user()
                flash("🎉 密码修改成功！请使用新密码重新登录。")
                return redirect(url_for('login'))

            return redirect(url_for('config'))

        # ====== ✨ 新增：处理网页端口保存请求 ======
        if form_type == 'web_settings':
           current_user.web_port = request.form.get('web_port').strip()
           current_user.save()  # 自动写入 config/users.json
           flash("🎉 网页项目访问端口保存成功！")
           return redirect(url_for('config'))

        protocol = request.form.get('protocol')
        host = request.form.get('host').strip().rstrip('/')
        port = request.form.get('port').strip()
        jf_username = request.form.get('jf_username')
        jf_password = request.form.get('jf_password')

        # 如果用户直接在 host 里输入了 http://，做一下容错清理
        if host.startswith('http://') or host.startswith('https://'):
            host = host.split('://')[-1]

        # 拼接出完整的 Jellyfin 基础 URL
        base_url = f"{protocol}://{host}:{port}"

        # Jellyfin 标准的登录授权接口
        auth_url = f"{base_url}/Users/AuthenticateByName"

        # 伪装成一个合法的客户端设备
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
                # 提取换回的 Token 和 UserId
                access_token = data.get("AccessToken")
                user_id = data.get("User", {}).get("Id")

                # 保存到当前用户的名下
                current_user.jellyfin_url = base_url
                current_user.jellyfin_api_key = access_token
                current_user.jellyfin_user_id = user_id

                # 更新至 JSON 文件
                current_user.save()

                flash("🎉 Jellyfin 服务器绑定成功！现在可以去拉取数据了。")
            else:
                flash(f"绑定失败：Jellyfin 账号或密码错误 (错误码: {resp.status_code})")

        except requests.exceptions.RequestException as e:
            flash(f"连接失败：无法访问该地址，请检查 IP、端口或网络是否互通。")

        return redirect(url_for('config'))

    return render_template('config.html', title="配置管理")


# ==========================================
# 🔍 TMDB 探索检索中心 (名字层级碰撞深度对齐)
# ==========================================
@app.route('/explore')
@login_required
def explore():
    """渲染探索搜索页面"""
    return render_template('explore.html', title="探索发现")


@app.route('/explore_detail/<media_type>/<int:item_id>')
@login_required
def explore_detail(media_type, item_id):
    """TMDB 探索结果详情页路由 (完美支持类型、年龄分级、播放进度与具体时间打标)"""

    api_key = current_user.tmdb_api_key
    if not api_key:
        flash("⚠️ 请先在配置管理中绑定 TMDB API Key！")
        return redirect(url_for('explore'))

    if media_type not in ['movie', 'tv']:
        flash("❌ 未知的媒体类型")
        return redirect(url_for('explore'))

    cache_key = f"{media_type}_{item_id}"
    current_time = time.time()
    render_data = None

    # ====== ✨ 1. 读取基础 TMDB 缓存 ======
    if cache_key in TMDB_DETAIL_CACHE:
        cached_item = TMDB_DETAIL_CACHE[cache_key]
        if current_time - cached_item['timestamp'] < CACHE_TTL:
            render_data = cached_item['data']
        else:
            del TMDB_DETAIL_CACHE[cache_key]

    # ====== ✨ 2. 如果没命中缓存，老老实实跨洋请求 TMDB ======
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
                flash(f"❌ 获取详情失败 (TMDB 状态码: {resp.status_code})")
                return redirect(url_for('explore'))

            data = resp.json()

            title = data.get('title') if media_type == 'movie' else data.get('name')
            overview = data.get('overview') or "这似乎是一部很神秘的影视作品，未抓取到相关的剧情介绍。"
            date_str = data.get('release_date') if media_type == 'movie' else data.get('first_air_date')
            year = date_str[:4] if date_str else "未知"

            poster_path = data.get('poster_path')
            poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else url_for('static',
                                                                                                     filename='images/logo.png')
            backdrop_path = data.get('backdrop_path')
            bg_url = f"https://image.tmdb.org/t/p/w1280{backdrop_path}" if backdrop_path else poster_url
            display_type = 'series' if media_type == 'tv' else 'movie'

            # 提取影视类型
            genres_list = [g.get('name') for g in data.get('genres', []) if g.get('name')]
            genres_str = ", ".join(genres_list) if genres_list else "未知类型"

            # 提取年龄分级
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
                for s in raw_seasons:
                    s_num = s.get('season_number')
                    if s_num is None: continue

                    # 请求单季详细列表
                    s_url = f"https://api.themoviedb.org/3/tv/{item_id}/season/{s_num}"
                    s_resp = requests.get(s_url, params={"api_key": api_key, "language": "zh-CN"},
                                          proxies=get_user_proxies(current_user), timeout=5)

                    if s_resp.status_code == 200:
                        s_data = s_resp.json()
                        episodes = s_data.get('episodes', [])

                        formatted_episodes = []
                        for ep in episodes:
                            still_path = ep.get('still_path')
                            full_still_url = f"https://image.tmdb.org/t/p/w300{still_path}" if still_path else url_for(
                                'static', filename='images/logo.png')

                            formatted_episodes.append({
                                'episode_num': ep.get('episode_number'),
                                'title': ep.get('name'),
                                'overview': ep.get('overview'),
                                'still_path': full_still_url,
                                'air_date': ep.get('air_date') or '未知首播时间'
                            })

                        seasons_data[s_num] = formatted_episodes
                        s_poster = s.get('poster_path')
                        season_poster_map[
                            s_num] = f"https://image.tmdb.org/t/p/w300{s_poster}" if s_poster else poster_url
                        season_overview_map[s_num] = s.get('overview') or s_data.get('overview') or ""

            render_data = {
                'title': title,
                'media_type': display_type,
                'year': year,
                'genres': genres_str,
                'rating': rating,
                'overview': overview,
                'poster_url': poster_url,
                'bg_url': bg_url,
                'seasons': seasons_data,
                'season_poster_map': season_poster_map,
                'season_overview_map': season_overview_map
            }

            # 存入缓存
            TMDB_DETAIL_CACHE[cache_key] = {
                'timestamp': current_time,
                'data': render_data
            }
        except Exception as e:
            flash(f"❌ 网络请求失败: {str(e)}")
            return redirect(url_for('explore'))

    # ====== ✨ 3. 核心大招：带着 TMDB 的名字，去你本地 SQLite 里查水表 ======
    # 这段绝不缓存，确保每次点进来都是最新的观看记录
    is_movie_watched = False
    is_series_watched = False  # ✨ 修复：初始化剧集全看完状态标识

    watched_episodes_dict = {}  # 记录单集及其观看时间
    season_watch_status = {}  # 记录整季观看状态："full" 或 "partial"
    has_watched_any = False  # 记录是否有观看过，用于显示顶部整体"正在追剧中"徽章

    import re
    title_to_check = render_data['title']

    if render_data['media_type'] == 'movie':
        # 如果是电影，直接去海报缓存表里捞
        movie_exist = db.session.query(WatchPoster.id).filter_by(
            user_id=current_user.id, media_type='Movie', display_title=title_to_check, is_deleted=False
        ).first()
        if movie_exist:
            is_movie_watched = True
    else:
        # 如果是剧集，去足迹明细表里把所有名字匹配的单集全捞出来
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
                # 记录具体哪一季哪一集看过了，并提取播放时间
                date_str = ep.date_played.strftime('%Y-%m-%d %H:%M') if ep.date_played else "未知时间"
                watched_episodes_dict[f"{s_num}_{e_num}"] = date_str

        # 核心算法：循环判断每一季的观看进度 VS TMDB 的总集数
        seasons_dict = render_data.get('seasons', {})
        for s_num, eps_list in seasons_dict.items():
            total_eps = len(eps_list)
            if total_eps == 0:
                continue

            # 统计这一季里，你的观看足迹命中了多少集
            watched_count = sum(
                1 for ep_info in eps_list if f"{s_num}_{ep_info.get('episode_num')}" in watched_episodes_dict)

            if watched_count > 0:
                has_watched_any = True
                # 全部看完给 full，看了一部分给 partial
                if watched_count >= total_eps:
                    season_watch_status[s_num] = "full"
                else:
                    season_watch_status[s_num] = "partial"

        # ====== ✨ 修复：新增剧集是否全看完的判断逻辑 ======
        if has_watched_any:
            is_series_watched = True
            valid_s_count = 0
            for s_num, eps_list in seasons_dict.items():
                # 只判断正片（排除第0季特别篇的干扰）
                if s_num > 0 and len(eps_list) > 0:
                    valid_s_count += 1
                    if season_watch_status.get(s_num) != "full":
                        is_series_watched = False
                        break

            # 如果这部剧全都是特别篇，没有正片，则兜底判断特别篇
            if valid_s_count == 0:
                for s_num, eps_list in seasons_dict.items():
                    if len(eps_list) > 0 and season_watch_status.get(s_num) != "full":
                        is_series_watched = False
                        break

    return render_template('explore_detail.html',
                           is_movie_watched=is_movie_watched,
                           is_series_watched=is_series_watched,  # ✨ 修复：传递新计算的状态给前端
                           watched_episodes_dict=watched_episodes_dict,
                           season_watch_status=season_watch_status,
                           has_watched_any=has_watched_any,
                           **render_data)



def download_tmdb_image(url, folder, filename, user_proxies=None):
    """辅助函数：安全下载 TMDB 影视图片到本地 static 目录"""
    try:
        os.makedirs(folder, exist_ok=True)
        filepath = os.path.join(folder, filename)
        # 如果本地已存在该图片，直接复用，避免重复请求
        if os.path.exists(filepath):
            return filename

        resp = requests.get(url, proxies=user_proxies, timeout=15)
        if resp.status_code == 200:
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            return filename
    except Exception as e:
        logger.error(f"TMDB 图片本地化失败: {e}")
    return None


@app.route('/api/explore/mark_watched', methods=['POST'])
@login_required
def api_mark_watched():
    """探索页专属 API：手动将 TMDB 数据逆向同步至本地 SQLite 观看历史并完整刮削季、集元数据"""
    api_key = current_user.tmdb_api_key
    if not api_key:
        return jsonify({"success": False, "message": "未绑定 TMDB API Key"})

    req_data = request.json or {}
    media_type = req_data.get('media_type')  # 'movie' 或 'series' / 'tv'
    if media_type == 'tv':
        media_type = 'series'

    item_id = req_data.get('item_id')
    scope = req_data.get('scope')  # 粒度范围: 'movie', 'series', 'season', 'episode', 'episode_batch'
    target_season = req_data.get('season_num')
    target_episode = req_data.get('episode_num')

    if not media_type or not item_id or not scope:
        return jsonify({"success": False, "message": "缺少必要请求参数"})

    proxies = get_user_proxies(current_user)
    now = datetime.now()

    try:
        # 1. 实时向 TMDB 索取底层元数据，确保抓取到正确的官方中文名、海报路径及简介
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

        # 2. 同步下载主海报墙所需的图片到本地
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

        # 3. 建立 TMDB 季元数据映射池
        tmdb_seasons_map = {}
        if media_type == 'series':
            for s in data.get('seasons', []):
                s_num = s.get('season_number')
                if s_num is not None:
                    tmdb_seasons_map[s_num] = {
                        'poster_path': s.get('poster_path'),
                        'overview': s.get('overview') or ""
                    }

        # 核心组件 A：动态创建/更新【季海报与季详情】记录的内部闭包函数
        def ensure_season_poster(s_num):
            target_id = f"{item_id}_S{s_num}"
            # ✨ 修复防撞：不再过滤 is_deleted=False，直接通过 target_id 捞取记录
            poster_record = WatchPoster.query.filter_by(
                user_id=current_user.id, target_id=target_id
            ).first()

            if poster_record:
                # ✨ 如果海报存在，直接将其“复活”并更新最后观看时间
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

        # 核心组件 B：动态创建【单集详情与剧照本地化】缓存的内部闭包函数
        def ensure_episode_detail(s_num, e_num, ep_name, ep_overview, ep_still_path):
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

        # 4. 电影单独创建主海报记录
        if media_type == 'movie':
            # ✨ 修复防撞：电影海报记录判断
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

        # 5. 根据不同的 scope 进行多层级级联写入 WatchRecord 历史足迹表与单集缓存
        if scope == 'movie':
            # ✨ 修复防撞：通过唯一的 item_id 查找，不限制 is_deleted
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

            # ✨ 修复防撞：单集复活
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

                    # ✨ 修复防撞：批量选择时的单集复活
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

                        # ✨ 修复防撞：整部/整季勾选时的批量复活
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
        return jsonify({"success": True, "message": "已成功同步并下载本地海报与元数据！"})

    except Exception as e:
        db.session.rollback()
        logger.error(f"反向同步操作失败 (用户: {current_user.username}): {str(e)}")
        return jsonify({"success": False, "message": f"反向同步操作失败: {str(e)}"})

@app.route('/api/search_tmdb')
@login_required
def api_search_tmdb():
    """TMDB 异步搜索接口：带内存缓存防限速，并与本地库进行实时碰撞对比"""
    query = request.args.get('q')
    if not query:
        return jsonify({"success": False, "message": "搜索词不能为空"})

    api_key = current_user.tmdb_api_key
    if not api_key:
        return jsonify({"success": False, "message": "请先在配置管理中绑定 TMDB API Key"})

    current_time = time.time()
    raw_results = None

    # ====== ✨ 核心逻辑 1：检查短时缓存 ======
    if query in TMDB_SEARCH_CACHE:
        cached_item = TMDB_SEARCH_CACHE[query]
        # 判断缓存是否过期
        if current_time - cached_item['timestamp'] < CACHE_TTL:
            raw_results = cached_item['data']
            logger.info(f"命中搜索缓存: {query}")
        else:
            # 缓存过期，清理掉
            del TMDB_SEARCH_CACHE[query]

    # ====== ✨ 核心逻辑 2：缓存未命中，发起真实网络请求 ======
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

                # 请求成功，存入内存缓存
                TMDB_SEARCH_CACHE[query] = {
                    'timestamp': current_time,
                    'data': raw_results
                }
                logger.info(f"走网络请求并写入缓存: {query}")
            else:
                return jsonify({"success": False, "message": f"TMDB 返回异常 (状态码: {resp.status_code})"})

        except Exception as e:
            return jsonify({"success": False, "message": f"网络请求失败，请检查代理配置: {str(e)}"})

    # 如果网络请求成功但没数据，直接返回空
    if not raw_results:
        return jsonify({"success": True, "results": []})

        # ====== ✨ 核心逻辑 3：本地数据库实时高匿碰撞 ======
    try:
        local_movies = db.session.query(WatchPoster.display_title) \
            .filter(WatchPoster.user_id == current_user.id, WatchPoster.media_type == 'Movie',
                    WatchPoster.is_deleted == False).all()
        watched_movies_set = {r[0] for r in local_movies if r[0]}

        local_series = db.session.query(WatchPoster.series_name) \
            .filter(WatchPoster.user_id == current_user.id, WatchPoster.media_type == 'Series',
                    WatchPoster.is_deleted == False).all()
        watched_series_set = {r[0] for r in local_series if r[0]}

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

                    # ✨ 新增：三态判定 (none/watched/watching)
                watch_status = 'none'

                if media_type == 'movie':
                    if tmdb_name in watched_movies_set:
                        watch_status = 'watched'
                else:
                    if tmdb_name in watched_series_set:
                        total_eps = 0
                        if item_id in TMDB_TV_EP_COUNT_CACHE:
                            total_eps = TMDB_TV_EP_COUNT_CACHE[item_id]
                        else:
                            try:
                                tv_resp = requests.get(f"https://api.themoviedb.org/3/tv/{item_id}",
                                                       params={"api_key": api_key},
                                                       proxies=get_user_proxies(current_user), timeout=3)
                                if tv_resp.status_code == 200:
                                    total_eps = tv_resp.json().get('number_of_episodes', 0)
                                    TMDB_TV_EP_COUNT_CACHE[item_id] = total_eps
                            except:
                                pass

                        ep_records = WatchRecord.query.filter_by(user_id=current_user.id, item_type='Episode',
                                                                 series_name=tmdb_name, is_deleted=False).all()
                        watched_normal_eps = set()
                        for ep in ep_records:
                            s_num = 1
                            if ep.season_name:
                                match = re.search(r'\d+', ep.season_name)
                                if match: s_num = int(match.group())
                            if s_num > 0 and ep.episode_num is not None:
                                watched_normal_eps.add(f"{s_num}_{ep.episode_num}")

                        local_count = len(watched_normal_eps)

                        # ✨ 修复：增加 local_count > 0 判断，防止 0 集时误判为 watching
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
                    'watch_status': watch_status  # ✨ 传递新的三态变量
                })

        return jsonify({"success": True, "results": results})

    except Exception as e:
        return jsonify({"success": False, "message": f"本地数据碰撞异常: {str(e)}"})

@app.route('/watched')
@login_required
def watched():
    """读取本地缓存：利用新增的纯剧集主海报与名字进行全局无重复渲染"""
    # 保持时间升序：时间越新的记录越靠后遍历，从而在字典赋值时自动覆盖旧记录
    all_posters = WatchPoster.query.filter_by(user_id=current_user.id, is_deleted=False).order_by(
        WatchPoster.last_watched_date.asc()).all()

    aggregated_dict = {}

    for p in all_posters:
        if p.media_type == "Movie":
            # ✨ 优化 1：电影优先使用 tmdb_id 聚合，防止多源头碰撞，无则使用 target_id 兜底
            key = f"movie_{p.tmdb_id}" if p.tmdb_id else f"movie_{p.target_id}"
            name = p.display_title
            img_file = p.local_image_path  # 电影依然使用 local_image_path
        else:
            # ✨ 优化 2：剧集放弃多平台不合群的 target_id，改用全局唯一的 tmdb_id 聚合，无则用剧集名兜底
            # 这样无论记录来自 Jellyfin (UUID) 还是探索页 (TMDB数字ID)，都能完美合并为同一个方块！
            key = f"series_{p.tmdb_id}" if p.tmdb_id else f"series_{p.series_name}"
            name = p.series_name  # 直接提取新字段：纯剧集名字
            img_file = p.series_image_path or p.local_image_path  # 直接提取新字段：纯剧集主海报，防多季重复

        # 时间靠后的最新足迹会自动覆盖字典中的旧足迹，从而锁定最新观看时间与最新的主海报图片
        aggregated_dict[key] = {
            "id": p.id,
            "name": name,
            "type_icon": "🎬" if p.media_type == "Movie" else "📺",
            "local_img_url": url_for('static', filename=img_file) if img_file else url_for('static', filename='images/logo.png'),
            "date_actual": p.last_watched_date
        }

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


@app.route('/image/<item_id>')
@login_required
def proxy_image(item_id):
    img_url = f"{current_user.jellyfin_url}/Items/{item_id}/Images/Primary?fillHeight=450&fillWidth=300&quality=90"
    headers = {"X-Emby-Token": current_user.jellyfin_api_key}
    try:
        resp = requests.get(img_url, headers=headers, stream=True, timeout=10)
        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        return Response(resp.iter_content(chunk_size=1024), content_type=content_type)
    except:
        return "Not found", 404


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# 辅助函数：格式化 Jellyfin 的 UTC 时间，并转换为东八区时间
def format_jellyfin_date(date_str):
    if not date_str:
        return "未知时间"
    try:
        # Jellyfin 时间格式示例: 2026-05-20T14:30:00.0000000Z
        date_str = date_str.split('.')[0]  # 截去尾部的毫秒和 Z
        dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
        dt_local = dt + timedelta(hours=8)  # 转换为东八区时间
        return dt_local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return date_str




# ==========================================
# 辅助函数 1：时间解析
# ==========================================
def parse_jellyfin_date(date_raw):
    """解析 Jellyfin 时间字符串为东八区 datetime 对象"""
    if not date_raw:
        return None
    date_str = date_raw.split('.')[0]
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
        return dt + timedelta(hours=8)
    except ValueError:
        return None


# ==========================================
# 辅助函数 2：图片下载引擎
# ==========================================
def download_image(url, headers, local_path):
    """下载图片到本地，若已存在则跳过。返回下载是否成功的结果。"""
    if os.path.exists(local_path):
        return True
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            with open(local_path, 'wb') as f:
                f.write(resp.content)
            return True
    except Exception:
        pass
    return False


# ==========================================
# 辅助函数：智能获取 TMDB ID (带内存缓存防频控)
# ==========================================
def get_tmdb_id_smart(user, item, item_type, tmdb_cache):
    """优先从 Jellyfin 提取 TMDB ID，若无则跨洋向 TMDB 搜索兜底"""

    # 1. 优先尝试从 Jellyfin 的原生数据中提取
    if item_type == "Movie":
        tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
        query_title = item.get("Name")
        search_type = "movie"
        year = item.get("ProductionYear")
    else:
        # 对于剧集，Jellyfin 有时会把剧集的 ID 放在 SeriesProviderIds 里
        tmdb_id = item.get("SeriesProviderIds", {}).get("Tmdb")
        query_title = item.get("SeriesName")
        search_type = "tv"
        year = None  # 单集较难直接获取剧集首播年份

    if tmdb_id:
        return str(tmdb_id)

    # 2. 如果 Jellyfin 没刮削出 TMDB ID，准备兜底搜索
    if not query_title or not user.tmdb_api_key:
        return None

    # 检查缓存，防止对同一部未刮削的剧集疯狂重复搜索
    cache_key = f"{search_type}_{query_title}"
    if cache_key in tmdb_cache:
        return tmdb_cache[cache_key]

    # 3. 发起真实的 TMDB API 搜索请求
    try:
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

        # 完美挂载用户配置的 HTTP 代理
        resp = requests.get(url, params=params, proxies=get_user_proxies(user), timeout=5)
        if resp.status_code == 200:
            results = resp.json().get('results', [])
            if results:
                fetched_id = str(results[0].get('id'))
                tmdb_cache[cache_key] = fetched_id  # 存入缓存
                return fetched_id
    except Exception as e:
        logger.warning(f"TMDB ID 嗅探请求失败 (关键字: {query_title}): {str(e)}")
        pass  # 搜索失败则静默放过

    tmdb_cache[cache_key] = None  # 标记为找不到，避免下次循环重复搜索
    return None


def update_watch_record(user_id, item, item_type, lib_name, dt_local, tmdb_id):
    item_id = item["Id"]

    # ====== ✨ 修复 1：去掉 is_deleted=False 的过滤，将被软删除的记录也捞出来 ======
    record = WatchRecord.query.filter_by(user_id=user_id, item_id=item_id).first()

    if not record:
        record = WatchRecord(
            user_id=user_id, item_id=item_id, item_type=item_type, library_name=lib_name,
            title=item.get("Name", "未知"), date_played=dt_local, source="Jellyfin", tmdb_id=tmdb_id
        )
        if item_type == "Episode":
            if item_type == "Episode":
                record.series_name = item.get("SeriesName", "未知剧集")

                # ✨ 修复 2：智能提取真实的季名称
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

        # ====== ✨ 修复 2：如果记录存在但已被软删除，则将其“复活”并更新时间 ======
        if getattr(record, 'is_deleted', False):
            record.is_deleted = False
            record.date_played = dt_local
            updated = True

        # ====== 如果没被删，且 Jellyfin 那边传来的时间比本地新，则只更新时间 ======
        elif dt_local > record.date_played:
            record.date_played = dt_local
            updated = True

        return updated


# ==========================================
# 辅助函数 3：写入/更新【海报墙双缓存表】
# ==========================================
def update_watch_poster(user_id, jf_user_id, item, item_type, dt_local, jf_url, headers, poster_dir, backdrop_dir,
                        synced_names, tmdb_id):
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

    # ====== ✨ 修复 1：去掉 is_deleted=False 的过滤，将被软删除的海报记录也捞出来 ======
    poster_record = WatchPoster.query.filter_by(user_id=user_id, target_id=target_id).first()

    if not poster_record:
        series_relative_path = "images/logo.png"
        season_relative_path = "images/logo.png"

        # ====== 彻底修复：隔离单集简介，并使用正确的 Jellyfin UUID 抓取 ======
        if item_type == "Movie":
            overview = item.get("Overview") or ""
            season_overview = ""
        else:
            overview = ""
            season_overview = ""

            series_id = item.get("SeriesId")
            if series_id:
                try:
                    s_resp = requests.get(f"{jf_url}/Users/{jf_user_id}/Items/{series_id}?Fields=Overview",
                                          headers=headers, timeout=5)
                    if s_resp.status_code == 200:
                        overview = s_resp.json().get("Overview") or ""
                except Exception:
                    pass

            season_id = item.get("SeasonId")
            if season_id:
                try:
                    se_resp = requests.get(f"{jf_url}/Users/{jf_user_id}/Items/{season_id}?Fields=Overview",
                                           headers=headers, timeout=5)
                    if se_resp.status_code == 200:
                        season_overview = se_resp.json().get("Overview") or ""
                except Exception:
                    pass

        bg_source_id = item.get("SeriesId") if item_type == "Episode" else item["Id"]

        # 背景图下载
        backdrop_filename = f"{bg_source_id}_backdrop.jpg"
        backdrop_path = os.path.join(backdrop_dir, backdrop_filename)
        backdrop_relative_path = f"backdrops/{backdrop_filename}"
        if not download_image(f"{jf_url}/Items/{bg_source_id}/Images/Backdrop/0?maxWidth=1920", headers, backdrop_path):
            backdrop_relative_path = None

        background_filename = f"{bg_source_id}_background.jpg"
        background_path = os.path.join(backdrop_dir, background_filename)
        background_relative_path = f"backdrops/{background_filename}"
        if not download_image(f"{jf_url}/Items/{bg_source_id}/Images/Backdrop/1?maxWidth=1920", headers,
                              background_path):
            background_relative_path = None

        # 海报下载
        if item_type == "Movie":
            movie_path = os.path.join(poster_dir, f"{target_id}_main.jpg")
            series_relative_path = f"posters/{target_id}_main.jpg"
            if not download_image(f"{jf_url}/Items/{target_id}/Images/Primary?maxWidth=400", headers, movie_path):
                series_relative_path = "images/logo.png"
        else:
            series_id = item.get("SeriesId")
            series_path = os.path.join(poster_dir, f"{series_id}_main.jpg")
            series_relative_path = f"posters/{series_id}_main.jpg"
            if not download_image(f"{jf_url}/Items/{series_id}/Images/Primary?maxWidth=400", headers, series_path):
                series_relative_path = "images/logo.png"

            season_id = item.get("SeasonId")
            season_path = os.path.join(poster_dir, f"{target_id}_season.jpg")
            season_relative_path = f"posters/{target_id}_season.jpg"
            if season_id and download_image(f"{jf_url}/Items/{season_id}/Images/Primary?maxWidth=400", headers,
                                            season_path):
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
    else:
        # ====== ✨ 修复 2：如果海报记录已被软删除，将其“复活”并更新时间 ======
        if getattr(poster_record, 'is_deleted', False):
            poster_record.is_deleted = False
            poster_record.last_watched_date = dt_local
            synced_names.add(display_title)  # 复活的记录也加到提示列表里

        # ====== 如果没被删，且 Jellyfin 那边有更新的观看时间，则只更新时间 ======
        elif dt_local > poster_record.last_watched_date:
            poster_record.last_watched_date = dt_local

@app.route('/sync_history')
@login_required
def sync_history():
    jf_url, headers = current_user.jellyfin_url, {"X-Emby-Token": current_user.jellyfin_api_key}
    base_user_url = f"{jf_url}/Users/{current_user.jellyfin_user_id}"
    poster_dir = os.path.join(app.root_path, 'static', 'posters')
    still_dir = os.path.join(app.root_path, 'static', 'stills')
    backdrop_dir = os.path.join(app.root_path, 'static', 'backdrops')
    for d in [poster_dir, still_dir, backdrop_dir]:
        os.makedirs(d, exist_ok=True)

    sync_count, synced_names, tmdb_search_cache = 0, set(), {}
    # ✨ 新增：初始化去重集合
    processed_ids = set()
    try:
        views_resp = requests.get(f"{base_user_url}/Views", headers=headers, timeout=10)
        if views_resp.status_code != 200:
            flash("无法获取媒体库列表，同步失败。")
            return redirect(url_for('watched_list'))

        for view in views_resp.json().get("Items", []):
            items_resp = requests.get(
                f"{base_user_url}/Items", headers=headers,
                params={"ParentId": view["Id"], "Filters": "IsPlayed", "IncludeItemTypes": "Movie,Episode",
                        "Recursive": "true", "Limit": 2000,
                        "Fields": "UserData,SeriesName,SeriesId,SeasonId,ParentIndexNumber,Overview,ProviderIds,SeriesProviderIds,SeasonName"},
                timeout=15
            )
            if items_resp.status_code != 200: continue

            for item in items_resp.json().get("Items", []):
                # ====== ✨ 新增防重逻辑 ======
                item_id = item["Id"]
                if item_id in processed_ids:
                    continue
                processed_ids.add(item_id)
                # ==============================
                dt_local = parse_jellyfin_date(item.get("UserData", {}).get("LastPlayedDate"))
                if not dt_local: continue

                master_tmdb_id = get_tmdb_id_smart(current_user, item, item["Type"], tmdb_search_cache)
                if update_watch_record(current_user.id, item, item["Type"], view["Name"], dt_local, master_tmdb_id):
                    sync_count += 1

                    update_watch_poster(current_user.id, current_user.jellyfin_user_id, item, item["Type"], dt_local,
                                        jf_url, headers, poster_dir, backdrop_dir, synced_names, master_tmdb_id)

                if item["Type"] == "Episode":
                    update_episode_detail(item, jf_url, headers, still_dir, master_tmdb_id)

        # ✨ 单线程排队处理完后，最后统一提交到数据库
        db.session.commit()

        if sync_count > 0:
            names_html = "<ul style='margin: 10px 0 0 0; padding-left: 20px; text-align: left; max-height: 150px; overflow-y: auto; color: var(--text-main);'>" + "".join(
                [f"<li style='margin-bottom: 6px;'>{n}</li>" for n in sorted(synced_names)]) + "</ul>"
            flash(f"🎉 同步成功！已处理明细并缓存海报/剧照/背景图：{names_html}")
        else:
            flash("✨ 同步完成！本地海报及历史记录已是最新。")

    except Exception as e:
        logger.error(f"媒体库同步过程中发生网络异常: {str(e)}")
        flash(f"同步过程中发生网络异常: {str(e)}")

    return redirect(url_for('watched_list'))


@app.route('/api/sync_stream')
@login_required
def api_sync_stream():
    """SSE 实时同步流微服务接口"""
    jf_url = current_user.jellyfin_url
    headers = {"X-Emby-Token": current_user.jellyfin_api_key}
    base_user_url = f"{jf_url}/Users/{current_user.jellyfin_user_id}"

    poster_dir = os.path.join(app.root_path, 'static', 'posters')
    still_dir = os.path.join(app.root_path, 'static', 'stills')
    backdrop_dir = os.path.join(app.root_path, 'static', 'backdrops')

    for d in [poster_dir, still_dir, backdrop_dir]:
        os.makedirs(d, exist_ok=True)

    def generate():
        sync_count = 0
        synced_names = set()
        tmdb_search_cache = {}
        # ✨ 新增：初始化去重集合
        processed_ids = set()

        try:
            logger.info(f"用户 {current_user.username} 触发了前端实时全量同步流...")
            yield f"data: {json.dumps({'status': 'syncing', 'name': '正在请求媒体库列表...'})}\n\n"
            views_resp = requests.get(f"{base_user_url}/Views", headers=headers, timeout=10)

            if views_resp.status_code != 200:
                yield f"data: {json.dumps({'status': 'error', 'message': '无法获取媒体库列表'})}\n\n"
                return

            for view in views_resp.json().get("Items", []):
                view_name = view.get('Name', '未知库')
                yield f"data: {json.dumps({'status': 'syncing', 'name': f'准备扫描库: {view_name}'})}\n\n"

                items_resp = requests.get(
                    f"{base_user_url}/Items", headers=headers,
                    params={"ParentId": view["Id"], "Filters": "IsPlayed", "IncludeItemTypes": "Movie,Episode",
                            "Recursive": "true", "Limit": 2000,
                            "Fields": "UserData,SeriesName,SeriesId,SeasonId,ParentIndexNumber,Overview,ProviderIds,SeriesProviderIds,SeasonName"},
                    timeout=15
                )
                if items_resp.status_code != 200: continue

                for item in items_resp.json().get("Items", []):
                    # ====== ✨ 新增防重逻辑 ======
                    item_id = item["Id"]
                    if item_id in processed_ids:
                        continue
                    processed_ids.add(item_id)
                    # ==============================
                    dt_local = parse_jellyfin_date(item.get("UserData", {}).get("LastPlayedDate"))
                    if not dt_local: continue

                    # 提取优雅的展示名称推送给前端
                    display_name = item.get("Name", "未知")
                    if item["Type"] == "Episode":
                        series_name = item.get("SeriesName", "未知剧集")
                        display_name = f"{series_name} - {display_name}"

                    # ✨ 核心：在这里把正在处理的名字实时通过流吐给前端！
                    yield f"data: {json.dumps({'status': 'syncing', 'name': display_name})}\n\n"

                    # 核心刮削与入库逻辑
                    master_tmdb_id = get_tmdb_id_smart(current_user, item, item["Type"], tmdb_search_cache)

                    # 使用线程锁，防止 SQLite 疯狂并发写入导致 "database is locked"
                    with db_lock:
                        if update_watch_record(current_user.id, item, item["Type"], view["Name"], dt_local,
                                               master_tmdb_id):
                            sync_count += 1
                            update_watch_poster(current_user.id, current_user.jellyfin_user_id, item, item["Type"],
                                                dt_local,
                                                jf_url, headers, poster_dir, backdrop_dir, synced_names, master_tmdb_id)

                        if item["Type"] == "Episode":
                            update_episode_detail(item, jf_url, headers, still_dir, master_tmdb_id)

            # 扫描完毕后，统一提交数据库
            with db_lock:
                db.session.commit()
                logger.info(f"手动全量同步完成！共入库/更新 {sync_count} 条记录。")

            # 通知前端：全部搞定，可以刷新页面了
            yield f"data: {json.dumps({'status': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"
            logger.error(f"SSE 实时同步流异常终止: {str(e)}")

    # ✨ 使用 stream_with_context，确保 current_user 和 db.session 在生成器迭代时依然存活
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/watched_list')
@login_required
def watched_list():
    """历史记录展示路由：支持列表与海报双视图渲染"""

    # ====== 1. 获取数据库记录 ======
    # 提取当前用户的所有播放流水记录 (按时间倒序)
    records = WatchRecord.query.filter_by(user_id=current_user.id, is_deleted=False).order_by(WatchRecord.date_played.desc()).all()

    # 提取当前用户所有的海报缓存数据
    posters = WatchPoster.query.filter_by(user_id=current_user.id, is_deleted=False).all()

    # ====== 2. 建立海报映射字典 (规避 N+1 查询卡顿) ======
    movie_poster_map = {}
    series_poster_map = {}
    for p in posters:
        if p.media_type == "Movie":
            movie_poster_map[p.display_title] = p.local_image_path
        else:
            # 剧集墙展示整部剧的主海报即可
            series_poster_map[p.series_name] = p.series_image_path or p.local_image_path

    # ====== 3. 组装供前端渲染的复杂数据字典 ======
    library_data = {}

    for record in records:
        lib_name = record.library_name or "未分类媒体库"
        item_type = record.item_type
        date_played_str = record.date_played.strftime('%Y-%m-%d %H:%M')

        # 初始化当前媒体库的数据结构
        if lib_name not in library_data:
            library_data[lib_name] = {
                'episodes_tree': {},
                'movies': [],
                'series_posters': {}  # 用来存放这个库里所有剧集的海报映射
            }

        # [分支 A：处理电影]
        if item_type == "Movie":
            library_data[lib_name]['movies'].append({
                'id': record.id,  # ✨ 新增：传入电影足迹在数据库中的真实 ID
                'name': record.title,
                'date': date_played_str,
                # 塞入电影海报路径，若无则使用 logo 兜底
                'poster_path': movie_poster_map.get(record.title, "images/logo.png")
            })

            # [分支 B：处理剧集]
        else:
            series_name = getattr(record, 'series_name', record.title)

            # ✨ 修复 3：直接读取数据库中已经清洗好的 season_name，杜绝强行算作第1季
            season_name = getattr(record, 'season_name')
            if not season_name:
                season_name = "第 1 季"

            episode_name = record.title

            # 顺手把该剧的海报映射存进去
            library_data[lib_name]['series_posters'][series_name] = series_poster_map.get(series_name,
                                                                                          "images/logo.png")

            # 构建原有的剧集折叠树结构
            if series_name not in library_data[lib_name]['episodes_tree']:
                library_data[lib_name]['episodes_tree'][series_name] = {}

            if season_name not in library_data[lib_name]['episodes_tree'][series_name]:
                library_data[lib_name]['episodes_tree'][series_name][season_name] = []

            library_data[lib_name]['episodes_tree'][series_name][season_name].append({
                'id': record.id,
                'episode': episode_name,
                'date': date_played_str
            })

    return render_template('watched_list.html', library_data=library_data)


@app.route('/detail/<media_type>/<path:title>')
@login_required
def media_detail(media_type, title):
    season_poster_map, season_overview_map = {}, {}

    if media_type == 'series':
        poster_info = WatchPoster.query.filter_by(user_id=current_user.id, media_type='Series', series_name=title, is_deleted=False).first()
        for p in WatchPoster.query.filter_by(user_id=current_user.id, media_type='Series', series_name=title, is_deleted=False).all():
            if p.season_num:
                season_poster_map[p.season_num] = p.local_image_path
                season_overview_map[p.season_num] = p.season_overview or ""
    else:
        poster_info = WatchPoster.query.filter_by(user_id=current_user.id, media_type='Movie', display_title=title, is_deleted=False).first()

    if not poster_info: poster_info = WatchPoster()

    seasons, movie_record = {}, None

    if media_type == 'series':
        for ep in [r for r in WatchRecord.query.filter_by(user_id=current_user.id, item_type='Episode', is_deleted=False).all() if getattr(r, 'series_name', r.title) == title]:
            ep_detail = EpisodeDetail.query.filter_by(item_id=ep.item_id).first()
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

            if real_season_num not in seasons: seasons[real_season_num] = []
            seasons[real_season_num].append(ep)
        seasons = dict(sorted(seasons.items()))
        for s in seasons:
            # 按数据库里的真实集数 (episode_num) 从小到大排序。如果没拿到集数，就排到最后(9999)
            seasons[s].sort(key=lambda x: x.episode_num if x.episode_num is not None else 9999)
    else:
        movie_record = WatchRecord.query.filter_by(user_id=current_user.id, item_type='Movie', title=title, is_deleted=False).first()

    return render_template('detail.html', media_type=media_type, title=title, poster=poster_info, seasons=seasons, movie_record=movie_record, season_poster_map=season_poster_map, season_overview_map=season_overview_map)

def get_user_proxies(user):
    if user.proxy_url and user.proxy_port:
        proxy_addr = f"http://{user.proxy_url}:{user.proxy_port}"
        return {"http": proxy_addr, "https": proxy_addr}
    return None
# 在请求时这样使用：
# requests.get(url, headers=headers, proxies=get_user_proxies(current_user))

@app.route('/api/delete_history', methods=['POST'])
@login_required
def delete_history():
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

        # 先将足迹的软删除状态刷入数据库，方便下面做统计
        db.session.flush()

        # ✨ 新增：处理海报墙级联隐藏（如果电影/剧集的最后一条记录被删了，海报也跟着软删除）
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
        logger.info(f"用户 {current_user.username} 手动删除了 {len(records)} 条观影足迹及相关缓存。")
        return jsonify({'success': True, 'message': f'成功移除了 {len(records)} 条足迹'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'删除失败: {str(e)}'})


# ==========================================
# 🌟 日志管理面板路由及文件写入逻辑
# ==========================================

def log_print(msg):
    """全局自定义日志打印函数：控制台输出并追加到 logs/jellywall.log"""
    print(msg)

    # ✨ 确保 logs 文件夹存在
    log_dir = os.path.join(app.root_path, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, 'jellywall.log')

    with open(log_file_path, 'a', encoding='utf-8') as f:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        f.write(f"[{timestamp}] {msg}\n")


@app.route('/logs')
@login_required
def logs_view():
    """渲染终端界面"""
    return render_template('logs.html', title="日志管理")


@app.route('/api/log_stream')
@login_required
def log_stream():
    """实时读取本地日志文件推送到前端"""
    log_dir = os.path.join(app.root_path, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, 'jellywall.log')

    if not os.path.exists(log_file_path):
        open(log_file_path, 'a', encoding='utf-8').close()

    def generate():
        with open(log_file_path, 'r', encoding='utf-8') as f:
            # ✨ 1. 先读取历史记录，把最后 100 行吐给前端
            lines = f.readlines()
            for line in lines[-100:]:
                if line.strip():
                    yield f"data: {line.strip()}\n\n"

            # ✨ 2. 进入死循环，监听新写入的内容
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    # 极其关键：清除 EOF 标志，强制 Python 重新检查文件末尾
                    f.seek(0, 1)
                    continue

                if line.strip():
                    yield f"data: {line.strip()}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/demo')
@login_required
def demo_preview():
    """纯静态体验版详情页"""
    return render_template('demo_detail.html', title="详情页预览")


if __name__ == '__main__':
    # 1. 保持你原有的数据库表结构自动创建逻辑
    with app.app_context():
        db.create_all()

    # ✨ 启动前，加载一次所有定时任务
    refresh_scheduler_jobs()
    scheduler.start()

    # 2. 设置一个保底的默认端口
    run_port = 5000

    # 3. 在项目启动前，主动去扒一遍 config/users.json，找出你配置的自定义端口
    try:


        config_path = os.path.join(app.root_path, 'config', 'users.json')
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                users_data = json.load(f)
                # 遍历找到第一个设置了 web_port 的配置
                for u in users_data.values():
                    if u.get('web_port'):
                        run_port = int(u['web_port'])
                        break
    except Exception as e:
        logger.warning(f"读取自定义端口失败，将使用默认端口 5000。原因: {e}")

    # 4. 把读出来的端口喂给 Flask，让它监听所有 IP (0.0.0.0)
    logger.info(f"JellyWall 即将启动，运行端口: {run_port}")
    app.run(host='0.0.0.0', port=run_port, debug=True)


