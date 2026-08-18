# JellyWall 更新日志
## [**🌟 v1.0.5**](https://github.com/ZhuMengMeng666/JellyWall/releases/tag/v1.0.5)
**最新**
2026/08/18

**🚀 优化**
- feat: Dashboard 新增 GridStack 可调整布局：卡片可拖拽调大小/换位、自动重排，移动端自动禁用，localStorage 持久化
- feat: Dashboard 统计大数字滚动动画（0.8s easeOutCubic），减弱动效时直接显示
- feat: Dashboard 时间线改为相对时间（今天/昨天/N 天前，超 7 天回退原格式），滚动条主题化
- feat: Dashboard 新增"近30天观影"统计卡（电影部数/剧集集数），三张统计卡调整为 4/4/4 同排布局
- feat: 海报墙与已观看记录海报视图改用 Steam 风格 3D 倾斜悬停效果（凹向光标 15°、放大 1.1、镜面反光带、停留 0.1s 触发）
- refactor: GridStack 布局存储升级为带版本号格式（v2），旧版 localStorage 布局自动迁移至新默认布局
- refactor: 3D 倾斜逻辑抽为公共文件 static/js/poster-tilt.js，两页共用，减弱动效时自动禁用，导出快照期间自动复位


## [**🌟 v1.0.4**](https://github.com/ZhuMengMeng666/JellyWall/releases/tag/v1.0.4)
2026/08/14

**🚀 优化**
- feat: 登录/注册页“影帧”视觉升级：随机 1080p 背景、电影遮幅、毛玻璃卡片、输入图标、进入动画、页脚版本号
- feat: 登录页新增“忘记密码”提示弹窗


## [**🌟 v1.0.3**](https://github.com/ZhuMengMeng666/JellyWall/releases/tag/v1.0.3)
2026/08/07

**🐛 Bug修复**
- fix: 详情页重复单集按媒体库过滤/类型视图去重，JOJO 等双来源剧集不再重复显示

**🚀 优化**
- perf: 新增"关于"页面，展示版本信息与 GitHub 仓库入口
- perf: 历史记录页改为按需加载，首屏 HTML 从 1.7MB 降至 72KB
- perf: lucide 图标子集化，全站图标库从 402KB 降至 13KB
- perf: 动效减弱媒体查询、日志面板局部图标扫描、探索页背景图 image-set
- perf: 图片加载细节优化（decoding/fetchpriority/海报墙导出预加载）
- perf: 调试模式改由 FLASK_DEBUG 环境变量控制，生产默认关闭
- perf: Watcharr 导入改为批量提交，减少事务开销
- perf: TMDB 缓存加 LRU 上限，防止内存无限增长
- perf: 背景图下载加 quality 压缩，单图体积降 40~60%
- perf: 海报墙与历史页只加载渲染所需列
- perf: 探索详情并发拉取各季，多季剧页面提速
- perf: 同步引擎统一核心并复用连接池，网络抖动自动重试
- perf: 仪表板热力图改为 SQL 分组聚合
- perf: 全项目日志统一格式与级别分类，补充关键操作日志

**📚 文档更新**
- docs: README 补充生产部署调试模式说明

## [**🌟 v1.0.1**](https://github.com/ZhuMengMeng666/JellyWall/releases/tag/v1.0.1)
2026/08/05

**🐛 Bug修复**
- fix: 修复部署时图片缓存目录缺失与未持久化问题
- fix: 调整 static/images 卷映射，避免覆盖镜像内 logo
- fix: 日志面板按日志自身时间分钟分组

**📚 文档更新**
- docs: 性能优化与项目文档完善

## [**🌟 v1.0.0**](https://github.com/ZhuMengMeng666/JellyWall/releases/tag/v1.0.0)
2026/08/04

**🚀 优化**
- feat: 发布初始版本，新增版本号与项目 README
