from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, Response, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import uuid
import os
from datetime import datetime, timedelta
import requests
from werkzeug.security import generate_password_hash  # 确保顶部引入了加密库

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
    # 新增 HTTP 代理字段
    proxy_url = db.Column(db.String(255), nullable=True)
    proxy_port = db.Column(db.String(10), nullable=True)

    tmdb_api_key = db.Column(db.String(255), nullable=True)

class WatchRecord(db.Model):
    """本地观影记录明细表"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

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

    __table_args__ = (db.UniqueConstraint('user_id', 'item_id', name='_user_item_uc'),)


class WatchPoster(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
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
        if form_type == 'proxy_settings':
            current_user.proxy_url = request.form.get('proxy_url').strip()
            current_user.proxy_port = request.form.get('proxy_port').strip()
            db.session.commit()
            flash("🎉 代理配置保存成功！")
            return redirect(url_for('config'))
        # 新增 TMDB 保存逻辑
        if form_type == 'tmdb_settings':
            current_user.tmdb_api_key = request.form.get('tmdb_api_key').strip()
            db.session.commit()
            flash("🎉 TMDB 密钥保存成功！")
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


# ==========================================
# 🔍 TMDB 探索检索中心 (名字层级碰撞深度对齐)
# ==========================================
@app.route('/explore')
@login_required
def explore():
    """渲染探索搜索页面"""
    return render_template('explore.html', title="探索发现")


@app.route('/api/search_tmdb')
@login_required
def api_search_tmdb():
    """TMDB 异步搜索接口：根据[影视中文名字]进行本地高匿碰撞比对"""
    query = request.args.get('q')
    if not query:
        return jsonify({"success": False, "message": "搜索词不能为空"})

    api_key = current_user.tmdb_api_key
    if not api_key:
        return jsonify({"success": False, "message": "请先在配置管理中绑定 TMDB API Key"})

    try:
        url = "https://api.themoviedb.org/3/search/multi"
        params = {
            "api_key": api_key,
            "query": query,
            "language": "zh-CN",
            "page": 1,
            "include_adult": "false"
        }

        # 完美引用用户自定义的全局 HTTP 代理进行中转请求
        resp = requests.get(url, params=params, proxies=get_user_proxies(current_user), timeout=10)

        if resp.status_code == 200:
            data = resp.json()
            raw_results = data.get('results', [])

            if not raw_results:
                return jsonify({"success": True, "results": []})

            # ====== ✨ 核心逻辑：从本地缓存表提取该用户所有的[电影名]和[纯剧集名] ======
            # 1. 提取已观看的电影名称集合 (对应 display_title)
            local_movies = db.session.query(WatchPoster.display_title) \
                .filter(WatchPoster.user_id == current_user.id, WatchPoster.media_type == 'Movie').all()
            watched_movies_set = {r[0] for r in local_movies if r[0]}

            # 2. 提取已观看的纯剧集名字集合 (对应新字段 series_name)
            local_series = db.session.query(WatchPoster.series_name) \
                .filter(WatchPoster.user_id == current_user.id, WatchPoster.media_type == 'Series').all()
            watched_series_set = {r[0] for r in local_series if r[0]}

            results = []
            for item in raw_results:
                media_type = item.get('media_type')
                if media_type in ['movie', 'tv']:
                    # 获取中文核心文本名字
                    tmdb_name = item.get('title') if media_type == 'movie' else item.get('name')
                    if not tmdb_name:
                        continue

                    date = item.get('release_date') if media_type == 'movie' else item.get('first_air_date')
                    poster_path = item.get('poster_path')

                    # 执行严格的[名字级]数据库命中判断
                    is_watched = False
                    if media_type == 'movie':
                        is_watched = tmdb_name in watched_movies_set
                    else:
                        is_watched = tmdb_name in watched_series_set

                    results.append({
                        'id': item.get('id'),
                        'media_type': media_type,  # 直接透传原始类型，由前端控制左上角输出
                        'title': tmdb_name,
                        'date': date[:4] if date else "未知年份",
                        'poster_url': f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None,
                        'is_watched': is_watched  # 碰撞比对成功为 True，前端直接渲染绿色对勾标章
                    })

            return jsonify({"success": True, "results": results})
        else:
            return jsonify({"success": False, "message": f"TMDB 返回异常 (状态码: {resp.status_code})"})

    except Exception as e:
        return jsonify({"success": False, "message": f"网络请求失败，请检查代理配置: {str(e)}"})
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
        pass  # 搜索失败则静默放过

    tmdb_cache[cache_key] = None  # 标记为找不到，避免下次循环重复搜索
    return None


def update_watch_record(user_id, item, item_type, lib_name, dt_local, tmdb_id):
    """处理 WatchRecord 表逻辑，返回是否发生了变动（新增或时间更新）"""
    item_id = item["Id"]
    record = WatchRecord.query.filter_by(user_id=user_id, item_id=item_id).first()

    if not record:
        record = WatchRecord(
            user_id=user_id,
            item_id=item_id,
            item_type=item_type,
            library_name=lib_name,
            title=item.get("Name", "未知"),
            date_played=dt_local,
            source="Jellyfin", # ✨ 明确打上来源标签
            tmdb_id = tmdb_id  # ✨ 存入 TMDB ID
        )
        if item_type == "Episode":
            record.series_name = item.get("SeriesName", "未知剧集")
            record.season_name = f"第 {item.get('ParentIndexNumber', '?')} 季"

            # 提取 Jellyfin 数据里的集数
            ep_index = item.get('IndexNumber')
            record.title = f"第 {ep_index or '?'} 集 - {item.get('Name', '未知集名')}"

            # ✨ 尝试将集数转化为纯数字保存
            try:
                record.episode_num = int(ep_index) if ep_index is not None else None
            except ValueError:
                record.episode_num = None

        db.session.add(record)
        return True
    else:
        # 如果记录已存在，仅更新最新观看时间
        if dt_local > record.date_played:
            record.date_played = dt_local
            return True

    return False


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

    poster_record = WatchPoster.query.filter_by(user_id=user_id, target_id=target_id).first()

    if not poster_record:
        series_relative_path = "images/logo.png"
        season_relative_path = "images/logo.png"

        # ====== ✨ 彻底修复：隔离单集简介，并使用正确的 Jellyfin UUID 抓取 ======
        if item_type == "Movie":
            overview = item.get("Overview") or ""
            season_overview = ""
        else:
            overview = ""
            season_overview = ""

            series_id = item.get("SeriesId")
            if series_id:
                try:
                    # 使用真正的 jf_user_id 替换原本错误的本地 user_id
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
        if dt_local > poster_record.last_watched_date: poster_record.last_watched_date = dt_local


@app.route('/sync_history')
@login_required
def sync_history():
    jf_url, headers = current_user.jellyfin_url, {"X-Emby-Token": current_user.jellyfin_api_key}
    base_user_url = f"{jf_url}/Users/{current_user.jellyfin_user_id}"
    poster_dir, still_dir, backdrop_dir = os.path.join(app.root_path, 'static', 'posters'), os.path.join(app.root_path,
                                                                                                         'static',
                                                                                                         'stills'), os.path.join(
        app.root_path, 'static', 'backdrops')
    for d in [poster_dir, still_dir, backdrop_dir]: os.makedirs(d, exist_ok=True)

    sync_count, synced_names, tmdb_search_cache = 0, set(), {}
    try:
        views_resp = requests.get(f"{base_user_url}/Views", headers=headers, timeout=10)
        if views_resp.status_code != 200: flash("无法获取媒体库列表，同步失败。"); return redirect(
            url_for('watched_list'))

        for view in views_resp.json().get("Items", []):
            items_resp = requests.get(f"{base_user_url}/Items", headers=headers,
                                      params={"ParentId": view["Id"], "Filters": "IsPlayed",
                                              "IncludeItemTypes": "Movie,Episode", "Recursive": "true", "Limit": 2000,
                                              "Fields": "UserData,SeriesName,SeriesId,SeasonId,ParentIndexNumber,Overview,ProviderIds,SeriesProviderIds"},
                                      timeout=15)
            if items_resp.status_code != 200: continue

            for item in items_resp.json().get("Items", []):
                dt_local = parse_jellyfin_date(item.get("UserData", {}).get("LastPlayedDate"))
                if not dt_local: continue

                master_tmdb_id = get_tmdb_id_smart(current_user, item, item["Type"], tmdb_search_cache)
                if update_watch_record(current_user.id, item, item["Type"], view["Name"], dt_local, master_tmdb_id):
                    sync_count += 1

                    # ====== ✨ 修复：追加传入了 current_user.jellyfin_user_id ======
                    update_watch_poster(current_user.id, current_user.jellyfin_user_id, item, item["Type"], dt_local,
                                        jf_url, headers, poster_dir, backdrop_dir, synced_names, master_tmdb_id)

                if item["Type"] == "Episode": update_episode_detail(item, jf_url, headers, still_dir, master_tmdb_id)

        db.session.commit()
        if sync_count > 0:
            names_html = "<ul style='margin: 10px 0 0 0; padding-left: 20px; text-align: left; max-height: 150px; overflow-y: auto; color: var(--text-main);'>" + "".join(
                [f"<li style='margin-bottom: 6px;'>{n}</li>" for n in sorted(synced_names)]) + "</ul>"
            flash(f"🎉 同步成功！已处理明细并缓存海报/剧照/背景图：{names_html}")
        else:
            flash("✨ 同步完成！本地海报及历史记录已是最新。")
    except Exception as e:
        flash(f"同步过程中发生网络异常: {str(e)}")
    return redirect(url_for('watched_list'))


@app.route('/watched_list')
@login_required
def watched_list():
    """历史记录展示路由：支持列表与海报双视图渲染"""

    # ====== 1. 获取数据库记录 ======
    # 提取当前用户的所有播放流水记录 (按时间倒序)
    records = WatchRecord.query.filter_by(user_id=current_user.id).order_by(WatchRecord.date_played.desc()).all()

    # 提取当前用户所有的海报缓存数据
    posters = WatchPoster.query.filter_by(user_id=current_user.id).all()

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
                'series_posters': {}  # ✨ 用来存放这个库里所有剧集的海报映射
            }

        # [分支 A：处理电影]
        if item_type == "Movie":
            library_data[lib_name]['movies'].append({
                'name': record.title,
                'date': date_played_str,
                # ✨ 塞入电影海报路径，若无则使用 logo 兜底
                'poster_path': movie_poster_map.get(record.title, "images/logo.png")
            })

        # [分支 B：处理剧集]
        else:
            series_name = getattr(record, 'series_name', record.title)

            try:
                season_num = int(getattr(record, 'season_num', 1)) if getattr(record, 'season_num',
                                                                              1) is not None else 1
            except ValueError:
                season_num = 1
            season_name = f"第 {season_num} 季"
            episode_name = record.title

            # ✨ 顺手把该剧的海报映射存进去
            library_data[lib_name]['series_posters'][series_name] = series_poster_map.get(series_name,
                                                                                          "images/logo.png")

            # 构建原有的剧集折叠树结构
            if series_name not in library_data[lib_name]['episodes_tree']:
                library_data[lib_name]['episodes_tree'][series_name] = {}

            if season_name not in library_data[lib_name]['episodes_tree'][series_name]:
                library_data[lib_name]['episodes_tree'][series_name][season_name] = []

            library_data[lib_name]['episodes_tree'][series_name][season_name].append({
                'episode': episode_name,
                'date': date_played_str
            })

    return render_template('watched_list.html', library_data=library_data)


import re


@app.route('/detail/<media_type>/<path:title>')
@login_required
def media_detail(media_type, title):
    season_poster_map, season_overview_map = {}, {}

    if media_type == 'series':
        poster_info = WatchPoster.query.filter_by(user_id=current_user.id, media_type='Series', series_name=title).first()
        for p in WatchPoster.query.filter_by(user_id=current_user.id, media_type='Series', series_name=title).all():
            if p.season_num:
                season_poster_map[p.season_num] = p.local_image_path
                season_overview_map[p.season_num] = p.season_overview or ""
    else:
        poster_info = WatchPoster.query.filter_by(user_id=current_user.id, media_type='Movie', display_title=title).first()

    if not poster_info: poster_info = WatchPoster()

    seasons, movie_record = {}, None

    if media_type == 'series':
        for ep in [r for r in WatchRecord.query.filter_by(user_id=current_user.id, item_type='Episode').all() if getattr(r, 'series_name', r.title) == title]:
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
        movie_record = WatchRecord.query.filter_by(user_id=current_user.id, item_type='Movie', title=title).first()

    return render_template('detail.html', media_type=media_type, title=title, poster=poster_info, seasons=seasons, movie_record=movie_record, season_poster_map=season_poster_map, season_overview_map=season_overview_map)

def get_user_proxies(user):
    if user.proxy_url and user.proxy_port:
        proxy_addr = f"http://{user.proxy_url}:{user.proxy_port}"
        return {"http": proxy_addr, "https": proxy_addr}
    return None
# 在请求时这样使用：
# requests.get(url, headers=headers, proxies=get_user_proxies(current_user))

@app.route('/demo')
@login_required
def demo_preview():
    """纯静态体验版详情页"""
    return render_template('demo_detail.html', title="详情页预览")

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)