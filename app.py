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


@app.route('/config')
@login_required
def config():
    return render_template('config.html', title="用户配置")


@app.route('/watched')
@login_required
def watched():
    """全量海报墙：混合电影与剧集实体，时间戳聚合去重，由远及近全局排序"""
    url = f"{current_user.jellyfin_url}/Users/{current_user.jellyfin_user_id}/Items"
    headers = {"X-Emby-Token": current_user.jellyfin_api_key}

    # 策略：请求全量的 Movie 和 Episode 以获取底层最细粒度的时间戳
    params = {
        "Filters": "IsPlayed",
        "IncludeItemTypes": "Movie,Episode",
        "Recursive": "true",
        "Fields": "UserData",
        "Limit": 2000  # 扩大阈值，确保时间排序的全局完整性
    }

    # 使用字典池进行 O(1) 复杂度的去重与时间戳竞争
    poster_dict = {}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code == 200:
            items = response.json().get("Items", [])
            for item in items:
                item_type = item.get("Type")
                user_data = item.get("UserData", {})
                played_date_raw = user_data.get("LastPlayedDate")

                # 脏数据过滤：丢弃没有明确播放时间记录的幽灵实体
                if not played_date_raw:
                    continue

                # 时间精度解析：剥离纳秒级精度与 UTC Z 标识
                date_str = played_date_raw.split('.')[0]
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
                    # 时区对齐：将 UTC 零时区强制平移至东八区本地时间
                    dt_local = dt + timedelta(hours=8)
                except Exception:
                    continue

                if item_type == "Movie":
                    poster_dict[item["Id"]] = {
                        "id": item["Id"],
                        "name": item.get("Name", "未知电影"),
                        "date_obj": dt_local,
                        "type_icon": "🎬"
                    }

                elif item_type == "Episode":
                    series_id = item.get("SeriesId")
                    series_name = item.get("SeriesName", "未知剧集")

                    if series_id:
                        # 核心聚合逻辑：同一剧集只保留一张海报，且时间戳必须是最新看的那一集
                        if series_id not in poster_dict:
                            poster_dict[series_id] = {
                                "id": series_id,
                                "name": series_name,
                                "date_obj": dt_local,
                                "type_icon": "📺"
                            }
                        else:
                            # 时间戳竞争：如果当前解析的单集时间晚于字典中已记录的时间，则覆盖更新
                            if dt_local > poster_dict[series_id]["date_obj"]:
                                poster_dict[series_id]["date_obj"] = dt_local

        else:
            flash(f"获取媒体库数据遭遇 HTTP 异常，状态码: {response.status_code}")
    except Exception as e:
        flash(f"Jellyfin API 握手失败，连接超时或拒绝访问: {str(e)}")

    # 维度转换与全局排序：将去重后的字典值转化为列表
    # sort 的 key 函数基于 datetime 对象，reverse=False 确保时间由远及近（历史的最前，最近的垫底）
    sorted_posters = sorted(poster_dict.values(), key=lambda x: x["date_obj"], reverse=False)

    # 渲染前的数据格式化：将不可序列化的 datetime 对象降维成易读的字符串格式
    for poster in sorted_posters:
        poster["date_formatted"] = poster["date_obj"].strftime("%Y-%m-%d")

    return render_template('watched.html', title="观影海报编年史", movies=sorted_posters)


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


@app.route('/watched_list')
@login_required
def watched_list():
    """获取观影记录列表（按媒体库分类 + 剧集折叠）"""
    headers = {"X-Emby-Token": current_user.jellyfin_api_key}
    base_url = f"{current_user.jellyfin_url}/Users/{current_user.jellyfin_user_id}"

    # 最终传递给前端的数据结构：{ "动漫库": {"movies": [], "episodes_tree": {}}, "电影库": ... }
    library_data = {}

    try:
        # 1. 先获取该用户能看到的所有媒体库 (Views)
        views_resp = requests.get(f"{base_url}/Views", headers=headers, timeout=10)

        if views_resp.status_code == 200:
            views = views_resp.json().get("Items", [])

            # 2. 遍历每个媒体库，单独请求该库下的已观看内容
            for view in views:
                lib_id = view["Id"]
                lib_name = view["Name"]

                params = {
                    "ParentId": lib_id,  # 核心：限定只在这个媒体库中搜索
                    "Filters": "IsPlayed",
                    "IncludeItemTypes": "Movie,Episode",
                    "Recursive": "true",
                    "SortBy": "DatePlayed",
                    "SortOrder": "Descending",
                    "Fields": "UserData",
                    "Limit": 500
                }

                items_resp = requests.get(f"{base_url}/Items", headers=headers, params=params, timeout=10)
                if items_resp.status_code == 200:
                    items = items_resp.json().get("Items", [])

                    if not items:
                        continue  # 如果这个媒体库里没有已观看的内容，直接跳过，前端不展示

                    movies_list = []
                    episodes_tree = {}

                    # 3. 数据归类解析 (复用之前的层级折叠逻辑)
                    for item in items:
                        item_type = item.get("Type")
                        played_date = format_jellyfin_date(item.get("UserData", {}).get("LastPlayedDate"))

                        if item_type == "Movie":
                            movies_list.append({
                                "name": item.get("Name", "未知电影"),
                                "date": played_date
                            })

                        elif item_type == "Episode":
                            series_name = item.get("SeriesName", "未知剧集")
                            season_str = f"第 {item.get('ParentIndexNumber', '?')} 季"
                            episode_str = f"第 {item.get('IndexNumber', '?')} 集 - {item.get('Name', '未知集名')}"

                            if series_name not in episodes_tree:
                                episodes_tree[series_name] = {}
                            if season_str not in episodes_tree[series_name]:
                                episodes_tree[series_name][season_str] = []

                            episodes_tree[series_name][season_str].append({
                                "episode": episode_str,
                                "date": played_date
                            })

                    # 将解析好的数据存入该媒体库名下
                    if movies_list or episodes_tree:
                        library_data[lib_name] = {
                            "movies": movies_list,
                            "episodes_tree": episodes_tree
                        }
        else:
            flash(f"获取媒体库失败，状态码: {views_resp.status_code}")
    except Exception as e:
        flash(f"请求 Jellyfin 出错: {str(e)}")

    return render_template('watched_list.html', title="观看记录列表", library_data=library_data)
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)