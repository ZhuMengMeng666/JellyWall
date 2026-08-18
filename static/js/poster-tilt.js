// ==========================================
// 海报 3D 倾斜卡片（公共逻辑，供"海报墙"与"已观看记录-海报视图"共用）
// 效果：停留 0.1s 触发 → 放大 1.1 → 15° 凹向光标倾斜 + 镜面反光带
// 实现：事件委托（pointerover/out/move），适配动态渲染的海报
// 提示：镜面反光的 CSS（.poster-item::after）在各页面模板内，参数改动只需改本文件
// ==========================================
(function () {
    var MAX_TILT = 15;        // 最大倾斜角度（度）
    var CARD_SCALE = 1.1;     // 悬停放大倍数
    var HOVER_DELAY = 100;    // 鼠标持续停留多久才触发（ms）
    var SMOOTH_MS = 160;      // 放大过渡结束、切换跟手模式的时间（ms）
    var PERSPECTIVE = 800;    // 透视距离（px）
    var SHINE_RANGE = 60;     // 反光带随倾斜扫过的最大位移（%）

    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var stateMap = new WeakMap();
    var currentCard = null;
    var tiltDisabled = false;

    function getState(card) {
        var s = stateMap.get(card);
        if (!s) {
            s = { rafId: null, enterTimer: null, smoothTimer: null, active: false };
            stateMap.set(card, s);
        }
        return s;
    }

    function activate(card, s) {
        if (tiltDisabled || s.active) return;
        s.active = true;
        card.classList.add('poster-tilt-active');
        // 激活瞬间先平滑放大，再随移动进入倾斜
        card.style.transition = 'transform 0.15s ease-out, box-shadow 0.3s';
        card.style.willChange = 'transform';
        card.style.transform = 'scale(' + CARD_SCALE + ')';
        // 放大过渡结束后切回无 transform 过渡，保证倾斜跟手
        s.smoothTimer = setTimeout(function () {
            s.smoothTimer = null;
            if (s.active) card.style.transition = 'box-shadow 0.3s';
        }, SMOOTH_MS);
    }

    function deactivate(card, s) {
        s.active = false;
        if (s.enterTimer) { clearTimeout(s.enterTimer); s.enterTimer = null; }
        if (s.smoothTimer) { clearTimeout(s.smoothTimer); s.smoothTimer = null; }
        if (s.rafId) { cancelAnimationFrame(s.rafId); s.rafId = null; }
        card.classList.remove('poster-tilt-active');
        // 先恢复 CSS 过渡，再清除 transform，实现平滑回弹
        card.style.transition = '';
        card.style.willChange = '';
        card.style.transform = '';
        card.style.removeProperty('--shine-x');
        card.style.removeProperty('--shine-y');
    }

    // 供导出快照等场景临时禁用：false = 禁用并复位全部海报，true = 恢复
    window.__setPosterTilt = function (enabled) {
        tiltDisabled = !enabled;
        if (!enabled) {
            document.querySelectorAll('.poster-item').forEach(function (card) {
                var s = stateMap.get(card);
                if (s) deactivate(card, s);
            });
            currentCard = null;
        }
    };

    if (reduce) return;

    // pointerenter/leave 不冒泡，用 pointerover/out 做事件委托
    document.addEventListener('pointerover', function (e) {
        if (e.pointerType !== 'mouse' || tiltDisabled) return;
        var card = e.target.closest ? e.target.closest('.poster-item') : null;
        if (card === currentCard) return;
        // 从旧卡片移到新卡片：先复位旧的
        if (currentCard) deactivate(currentCard, getState(currentCard));
        currentCard = card;
        if (!card) return;
        var s = getState(card);
        // 鼠标持续停留 HOVER_DELAY 后才触发放大 + 倾斜
        s.enterTimer = setTimeout(function () { activate(card, s); }, HOVER_DELAY);
    }, true);

    document.addEventListener('pointermove', function (e) {
        if (e.pointerType !== 'mouse' || tiltDisabled) return;
        var card = e.target.closest ? e.target.closest('.poster-item') : null;
        if (!card) return;
        var s = getState(card);
        if (!s.active || s.rafId) return;
        s.rafId = requestAnimationFrame(function () {
            s.rafId = null;
            if (tiltDisabled || !s.active) return;
            var rect = card.getBoundingClientRect();
            if (!rect.width || !rect.height) return;
            // 光标相对卡片中心位置：-0.5 ~ 0.5
            var px = (e.clientX - rect.left) / rect.width - 0.5;
            var py = (e.clientY - rect.top) / rect.height - 0.5;
            // 光标所在边缘朝屏幕内凹陷（凹向光标方向）
            var rx = -py * MAX_TILT * 2;
            var ry = px * MAX_TILT * 2;
            // 镜面反光带随倾斜角度反向扫过表面，模拟镜面反射
            card.style.setProperty('--shine-x', (-(ry / MAX_TILT) * SHINE_RANGE).toFixed(1) + '%');
            card.style.setProperty('--shine-y', (-(rx / MAX_TILT) * SHINE_RANGE).toFixed(1) + '%');
            card.style.transform = 'scale(' + CARD_SCALE + ') perspective(' + PERSPECTIVE + 'px) rotateX(' + rx.toFixed(2) + 'deg) rotateY(' + ry.toFixed(2) + 'deg)';
        });
    }, true);

    document.addEventListener('pointerout', function (e) {
        if (e.pointerType !== 'mouse' || tiltDisabled) return;
        var card = e.target.closest ? e.target.closest('.poster-item') : null;
        if (currentCard && card !== currentCard) {
            deactivate(currentCard, getState(currentCard));
            currentCard = null;
        }
    }, true);
})();
