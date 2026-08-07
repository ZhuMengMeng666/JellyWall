// 生成 static/js/lucide.subset.min.js：从完整 lucide.min.js 提取模板实际用到的图标
// 用法：node tools/build_lucide_subset.mjs
// 注意：以后新增 data-lucide 图标后必须重跑本脚本，否则新图标渲染为空且控制台报
//       "icon name was not found" 警告（生成时会自动扫描并校验缺失图标）。
import fs from 'fs';
import path from 'path';
import { createRequire } from 'module';
import { execFileSync } from 'child_process';

const require = createRequire(import.meta.url);
const OUT = process.argv[2] || path.resolve('static/js/lucide.subset.min.js');

// 动态图标白名单：
// - 模板 JS 中以变量形式动态渲染的图标（如 <i data-lucide="${iconName}">），静态扫描扫不到取值；
// - 后端 Python 渲染函数动态生成的图标（如 CHANGELOG 分类图标），模板文件里也扫不到；
// 新增此类图标时同步补充
const DYNAMIC_ICONS = ['folder-open', 'bug', 'star', 'book-open'];

// 1) 收集模板用到的图标名（kebab-case）：静态扫描 + 动态白名单
const used = new Set(DYNAMIC_ICONS);
for (const t of fs.readdirSync('templates')) {
  if (!t.endsWith('.html')) continue;
  const c = fs.readFileSync(path.join('templates', t), 'utf8');
  for (const m of c.matchAll(/data-lucide="([a-z0-9-]+)"/g)) used.add(m[1]);
}

// 2) 校验模板只使用 lucide.createIcons（子集 shim 只实现该 API，且支持局部容器扫描）
for (const t of fs.readdirSync('templates')) {
  if (!t.endsWith('.html')) continue;
  const c = fs.readFileSync(path.join('templates', t), 'utf8');
  const uses = new Set();
  for (const m of c.matchAll(/lucide\.([A-Za-z]+)/g)) uses.add(m[1]);
  for (const u of uses) {
    // createIcons:页面调用;min/subset:仅出现在脚本文件名(lucide.min.js / lucide.subset.min.js)的 src 中
    if (u !== 'createIcons' && u !== 'min' && u !== 'subset') {
      throw new Error(`模板 ${t} 使用了 lucide.${u}，子集 shim 不支持，请改用 createIcons`);
    }
  }
}

// 3) 从完整版提取图标定义并渲染 SVG 内部节点（与官方渲染结果同源）
const lucide = require(path.resolve('static/js/lucide.min.js'));
const pascal = (name) => name.split('-').map((s) => s.charAt(0).toUpperCase() + s.slice(1)).join('');

function renderNode(node) {
  const [tag, attrs, ...children] = node;
  const a = Object.entries(attrs || {})
    .map(([k, v]) => ` ${k}="${String(v).replace(/&/g, '&amp;').replace(/"/g, '&quot;')}"`)
    .join('');
  const kids = children && children.length ? children.map(renderNode).join('') : '';
  return `<${tag}${a}>${kids}</${tag}>`;
}

const iconMap = {};
const missing = [];
for (const name of [...used].sort()) {
  const def = lucide.icons[pascal(name)];
  if (!def) {
    missing.push(name);
    continue;
  }
  iconMap[name] = def.map(renderNode).join('');
}
if (missing.length) {
  console.error('以下图标在 lucide v1.17 中不存在:', missing.join(', '));
  process.exit(1);
}

// 4) 生成子集文件：shim 实现 createIcons，兼容官方签名 createIcons(options, customElements)
//    （包含 logs.html 使用的 createIcons({}, [btnSysLogs]) 局部容器扫描）
const shim = `(function(){var E={xmlns:"http://www.w3.org/2000/svg",width:24,height:24,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor","stroke-width":2,"stroke-linecap":"round","stroke-linejoin":"round"},I=${JSON.stringify(iconMap)};window.lucide={createIcons:function(o,c){var scope;if(!c){scope=[document.body]}else if(Array.isArray(c)){scope=c}else if(c.length!==undefined){scope=Array.from(c)}else{scope=[c]}var els=[];scope.forEach(function(s){els=els.concat(Array.from(s.querySelectorAll('[data-lucide]')))});els.forEach(function(el){var n=el.getAttribute('data-lucide'),h=I[n];if(!h){console.warn(el.outerHTML+' icon name was not found in the provided icons object.');return}var a={},hasAria=!1;Array.prototype.forEach.call(el.attributes,function(x){if(x.name.indexOf('aria-')===0||x.name==='role'||x.name==='title')hasAria=!0});Object.keys(E).forEach(function(k){a[k]=E[k]});a['data-lucide']=n;if(!hasAria)a['aria-hidden']='true';var cls=['lucide','lucide-'+n];Array.prototype.forEach.call(el.attributes,function(x){if(x.name==='class'){cls=cls.concat(x.value.split(/\\s+/).filter(Boolean))}else{a[x.name]=x.value}});a['class']=cls.join(' ');var svg=document.createElementNS('http://www.w3.org/2000/svg','svg');Object.keys(a).forEach(function(k){svg.setAttribute(k,a[k])});svg.innerHTML=h;if(el.parentNode)el.parentNode.replaceChild(svg,el)})}}})();`;

fs.writeFileSync(OUT, shim, 'utf8');
execFileSync(process.execPath, ['--check', OUT]);
console.log('icons used:', used.size, '| subset size:', fs.statSync(OUT).size, 'bytes');
console.log('提示:以后新增 data-lucide 图标后请重跑: node tools/build_lucide_subset.mjs');
