from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = 'jellywall_super_secret_key_2026'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///project.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


# ================= 数据库模型 =================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)

    # 绑定的 Jellyfin 信息
    jellyfin_url = db.Column(db.String(255), nullable=True)
    jellyfin_api_key = db.Column(db.String(255), nullable=True)
    jellyfin_user_id = db.Column(db.String(255), nullable=True)

class WatchRecord(db.Model):
    """本地观影记录表"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    # 基础信息
    item_id = db.Column(db.String(100), nullable=False)  # Jellyfin 里的 ID
    item_type = db.Column(db.String(50), nullable=False)  # 'Movie' 或 'Episode'
    library_name = db.Column(db.String(100), nullable=False)  # 媒体库名称 (如"电影", "动漫")

    # 电影/剧集具体信息
    title = db.Column(db.String(200), nullable=False)  # 电影名 或 单集名
    series_name = db.Column(db.String(200))  # 剧集名 (仅剧集有)
    season_name = db.Column(db.String(100))  # 季名 (仅剧集有)

    # 播放时间 (存储为 datetime 对象方便排序)
    date_played = db.Column(db.DateTime, nullable=False)

    # 建立一个复合索引，加快查询速度，防止同一个用户存入重复的 item_id
    __table_args__ = (db.UniqueConstraint('user_id', 'item_id', name='_user_item_uc'),)


class WatchPoster(db.Model):
    """本地海报墙聚合表（升级版：支持季海报与剧集主海报双缓存）"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    target_id = db.Column(db.String(100), nullable=False)  # 电影itemId 或 剧集"SeriesId_S1"
    media_type = db.Column(db.String(50), nullable=False)  # 'Movie' 或 'Series'
    display_title = db.Column(db.String(200), nullable=False)  # 原始完整标题，如"动漫名 (第 1 季)"

    # ====== ✨ 新增字段 ======
    series_name = db.Column(db.String(200), nullable=True)  # 专门记录纯粹的【剧集名字】
    season_num = db.Column(db.Integer, nullable=True)  # 专门记录【第几季】的数字

    # ====== 🧭 海报路径重新分配 ======
    local_image_path = db.Column(db.String(255), nullable=False)  # 💥 存储【剧集季海报】或电影海报
    series_image_path = db.Column(db.String(255), nullable=True)  # 💥 存储【纯剧集主海报】

    last_watched_date = db.Column(db.DateTime, nullable=False)

    __table_args__ = (db.UniqueConstraint('user_id', 'target_id', 'display_title', name='_user_poster_uc'),)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ================= 路由逻辑 =================

@app.route('/')
def index():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    if not current_user.jellyfin_url or not current_user.jellyfin_api_key:
        return redirect(url_for('onboarding'))
    return redirect(url_for('dashboard'))


from werkzeug.security import check_password_hash
from flask_login import login_user


@app.route('/login', methods=['GET', 'POST'])
def login():
    # 如果已经登录过，直接跳到仪表板
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        # 1. 在数据库中寻找这个用户
        user = User.query.filter_by(username=username).first()

        # 2. 验证用户存在，并且密码正确
        if user and check_password_hash(user.password, password):
            # 3. 记录登录状态
            login_user(user)

            # 👇 这里就是登录成功后跳转的页面！通常是 dashboard（仪表板）
            return redirect(url_for('dashboard'))

        else:
            # 如果账号或密码错误，发送提示信息
            flash('用户名或密码错误，请重试。')

    # 如果是 GET 请求，或者密码验证失败，就重新渲染并停留在登录页
    return render_template('login.html')


from werkzeug.security import generate_password_hash  # 确保顶部引入了加密库


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

        # 检查用户名是否已存在
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash('该用户名已被注册，请换一个重试。')
            return redirect(url_for('register'))

        # 创建新用户并哈希密码 (根据你的 User 模型实际字段调整)
        new_user = User(
            username=username,
            password=generate_password_hash(password),
            # 如果你的数据库现在不需要绑 Jellyfin，这两行可以删掉，放到配置页再去绑
            jellyfin_url=jellyfin_url,
            jellyfin_api_key=jellyfin_api_key
        )

        db.session.add(new_user)
        db.session.commit()

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
                db.session.commit()
                return redirect(url_for('dashboard'))
            elif resp.status_code == 401:
                flash('绑定失败：Jellyfin 用户名或密码错误。')
            else:
                flash(f'绑定失败：服务器返回状态码 {resp.status_code}')
        except Exception as e:
            flash(f'无法连接到 Jellyfin，请检查网络或配置。详细: {str(e)}')

    return render_template('onboarding.html')


@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', title="首页")


import requests


@app.route('/config', methods=['GET', 'POST'])
@login_required
def config():
    if request.method == 'POST':
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

                # 保存到本地数据库当前用户的名下
                current_user.jellyfin_url = base_url
                current_user.jellyfin_api_key = access_token
                current_user.jellyfin_user_id = user_id
                db.session.commit()

                flash("🎉 Jellyfin 服务器绑定成功！现在可以去拉取数据了。")
            else:
                flash(f"绑定失败：Jellyfin 账号或密码错误 (错误码: {resp.status_code})")

        except requests.exceptions.RequestException as e:
            flash(f"连接失败：无法访问该地址，请检查 IP、端口或网络是否互通。")

        return redirect(url_for('config'))

    return render_template('config.html', title="配置管理")


@app.route('/watched')
@login_required
def watched():
    """读取本地缓存：利用新增的纯剧集主海报与名字进行全局无重复渲染"""
    all_posters = WatchPoster.query.filter_by(user_id=current_user.id).order_by(
        WatchPoster.last_watched_date.asc()).all()

    aggregated_dict = {}

    for p in all_posters:
        if p.media_type == "Movie":
            key = p.target_id
            name = p.display_title
            img_file = p.local_image_path  # 电影依然使用 local_image_path
        else:
            # 剧集：纯按剧集 ID（即 target_id 前缀）去重聚合
            pure_series_id = p.target_id.split('_')[0]
            key = pure_series_id
            name = p.series_name  # 💥 直接提取新字段：纯剧集名字
            img_file = p.series_image_path  # 💥 直接提取新字段：纯剧集主海报，彻底隔绝多季重复方块！

        aggregated_dict[key] = {
            "id": p.id,
            "name": name,
            "type_icon": "🎬" if p.media_type == "Movie" else "📺",
            "local_img_url": url_for('static', filename=img_file),
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


from datetime import datetime, timedelta

import os
from datetime import datetime, timedelta
import requests

import os
from datetime import datetime, timedelta
import requests


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
# 辅助函数 3：写入/更新【观影明细表】
# ==========================================
def update_watch_record(user_id, item, item_type, lib_name, dt_local):
    """处理 WatchRecord 表逻辑，返回是否发生了变动（新增或时间更新）"""
    item_id = item["Id"]
    record = WatchRecord.query.filter_by(user_id=user_id, item_id=item_id).first()

    if not record:
        record = WatchRecord(
            user_id=user_id, item_id=item_id, item_type=item_type,
            library_name=lib_name, title=item.get("Name", "未知"), date_played=dt_local
        )
        if item_type == "Episode":
            record.series_name = item.get("SeriesName", "未知剧集")
            record.season_name = f"第 {item.get('ParentIndexNumber', '?')} 季"
            record.title = f"第 {item.get('IndexNumber', '?')} 集 - {item.get('Name', '未知集名')}"
        db.session.add(record)
        return True
    else:
        if dt_local > record.date_played:
            record.date_played = dt_local
            return True
    return False


# ==========================================
# 辅助函数 4：写入/更新【海报墙双缓存表】
# ==========================================
def update_watch_poster(user_id, item, item_type, dt_local, jf_url, headers, poster_dir, synced_names):
    """处理 WatchPoster 表逻辑，并触发季海报与剧集主海报的下载"""
    item_id = item["Id"]

    # 提取聚合属性
    if item_type == "Movie":
        target_id = item_id
        media_type = "Movie"
        display_title = item.get("Name", "未知电影")
        pure_series_name, season_num_int = None, None
    else:
        series_id = item.get("SeriesId", item_id)
        raw_season_num = item.get("ParentIndexNumber")
        try:
            season_num_int = int(raw_season_num) if raw_season_num is not None else None
        except ValueError:
            season_num_int = None

        target_id = f"{series_id}_S{season_num_int or '?'}"
        media_type = "Series"
        pure_series_name = item.get("SeriesName", "未知剧集")
        display_title = f"{pure_series_name} (第 {season_num_int or '?'} 季)"

    # 操作海报墙数据库
    poster_record = WatchPoster.query.filter_by(user_id=user_id, target_id=target_id).first()

    if not poster_record:
        series_relative_path = "images/logo.png"
        season_relative_path = "images/logo.png"

        if item_type == "Movie":
            movie_path = os.path.join(poster_dir, f"{target_id}.jpg")
            season_relative_path = f"posters/{target_id}.jpg"
            img_url = f"{jf_url}/Items/{item_id}/Images/Primary?maxWidth=400"

            if not download_image(img_url, headers, movie_path):
                season_relative_path = "images/logo.png"
            synced_names.add(f"🎬《{display_title}》")

        else:  # Episode
            # 1. 下载主剧集海报
            series_id = item.get("SeriesId")
            series_path = os.path.join(poster_dir, f"{series_id}_main.jpg")
            series_relative_path = f"posters/{series_id}_main.jpg"
            s_url = f"{jf_url}/Items/{series_id}/Images/Primary?maxWidth=400"
            if not download_image(s_url, headers, series_path):
                series_relative_path = "images/logo.png"

            # 2. 下载季海报
            season_id = item.get("SeasonId")
            season_path = os.path.join(poster_dir, f"{target_id}_season.jpg")
            season_relative_path = f"posters/{target_id}_season.jpg"
            se_url = f"{jf_url}/Items/{season_id}/Images/Primary?maxWidth=400" if season_id else None

            if se_url and download_image(se_url, headers, season_path):
                pass  # 成功下载
            else:
                season_relative_path = series_relative_path  # 回退为主海报

            synced_names.add(f"📺《{pure_series_name}》第 {season_num_int or '?'} 季")

        # 写入新记录
        poster_record = WatchPoster(
            user_id=user_id, target_id=target_id, media_type=media_type, display_title=display_title,
            series_name=pure_series_name, season_num=season_num_int,
            local_image_path=season_relative_path,
            series_image_path=series_relative_path if media_type == "Series" else None,
            last_watched_date=dt_local
        )
        db.session.add(poster_record)
    else:
        # 更新已存在的海报观看时间
        if dt_local > poster_record.last_watched_date:
            poster_record.last_watched_date = dt_local


# ==========================================
# 🌟 主路由：逻辑编排层 (瞬间清爽)
# ==========================================
@app.route('/sync_history')
@login_required
def sync_history():
    """手动同步主控台：调用辅函数进行数据解析与落地"""
    jf_url = current_user.jellyfin_url
    headers = {"X-Emby-Token": current_user.jellyfin_api_key}
    base_user_url = f"{jf_url}/Users/{current_user.jellyfin_user_id}"

    poster_dir = os.path.join(app.root_path, 'static', 'posters')
    os.makedirs(poster_dir, exist_ok=True)

    sync_count = 0
    synced_names = set()

    try:
        views_resp = requests.get(f"{base_user_url}/Views", headers=headers, timeout=10)
        if views_resp.status_code != 200:
            flash("无法获取媒体库列表，同步失败。")
            return redirect(url_for('watched_list'))

        # 遍历媒体库
        for view in views_resp.json().get("Items", []):
            params = {
                "ParentId": view["Id"], "Filters": "IsPlayed", "IncludeItemTypes": "Movie,Episode",
                "Recursive": "true", "Fields": "UserData,SeriesName,SeriesId,SeasonId,ParentIndexNumber", "Limit": 2000
            }
            items_resp = requests.get(f"{base_user_url}/Items", headers=headers, params=params, timeout=15)
            if items_resp.status_code != 200:
                continue

            # 遍历观看记录项目
            for item in items_resp.json().get("Items", []):
                dt_local = parse_jellyfin_date(item.get("UserData", {}).get("LastPlayedDate"))
                if not dt_local: continue

                # 调用职能 1：处理明细
                is_updated = update_watch_record(current_user.id, item, item["Type"], view["Name"], dt_local)

                # 调用职能 2：处理海报
                if is_updated:
                    sync_count += 1
                    update_watch_poster(current_user.id, item, item["Type"], dt_local, jf_url, headers, poster_dir,
                                        synced_names)

        db.session.commit()

        # 弹窗提示逻辑
        if sync_count > 0:
            names_html = "<ul style='margin: 10px 0 0 0; padding-left: 20px; text-align: left; max-height: 150px; overflow-y: auto; color: var(--text-main);'>"
            for name in sorted(synced_names):
                names_html += f"<li style='margin-bottom: 6px;'>{name}</li>"
            names_html += "</ul>"
            flash(f"🎉 同步成功！已处理明细并缓存海报：{names_html}")
        else:
            flash("✨ 同步完成！本地海报及历史记录已是最新。")

    except Exception as e:
        flash(f"同步过程中发生网络异常: {str(e)}")

    return redirect(url_for('watched_list'))


@app.route('/watched_list')
@login_required
def watched_list():
    """从本地数据库读取观看记录（极速加载）"""
    # 直接从本地 SQLite 数据库中按时间倒序拉取当前用户的所有记录
    records = WatchRecord.query.filter_by(user_id=current_user.id).order_by(WatchRecord.date_played.desc()).all()

    library_data = {}

    # 将扁平的数据库记录重新组装成前端需要的折叠树结构
    for r in records:
        lib_name = r.library_name
        date_str = r.date_played.strftime("%Y-%m-%d %H:%M")

        if lib_name not in library_data:
            library_data[lib_name] = {"movies": [], "episodes_tree": {}}

        if r.item_type == "Movie":
            library_data[lib_name]["movies"].append({
                "name": r.title,
                "date": date_str
            })
        elif r.item_type == "Episode":
            series = r.series_name
            season = r.season_name

            if series not in library_data[lib_name]["episodes_tree"]:
                library_data[lib_name]["episodes_tree"][series] = {}
            if season not in library_data[lib_name]["episodes_tree"][series]:
                library_data[lib_name]["episodes_tree"][series][season] = []

            library_data[lib_name]["episodes_tree"][series][season].append({
                "episode": r.title,
                "date": date_str
            })

    return render_template('watched_list.html', title="同步记录", library_data=library_data)
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)