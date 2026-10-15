// Ported from the campfire SVG and progress rules in 篝火夜读代码.html.
// Every layer shares a 320 × 185 coordinate frame. Stack them in the order
// background, aurora, decor, glow, fire, scene; animate the transparent layers
// in WXSS because SVG-as-image does not run the reference's page CSS.
const VIEW_BOX = '0 0 320 185';
const DEFAULT_TOTAL = 0;
const BASE64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

const FIRE_PATHS = '<path d="M160 72 C135 98, 134 134, 160 144 C186 134, 185 98, 160 72 Z" fill="#F97316"/>'
  + '<path d="M160 88 C144 106, 143 134, 160 144 C177 134, 176 106, 160 88 Z" fill="#FACC15"/>'
  + '<path d="M160 108 C151 118, 151 136, 160 144 C169 136, 169 118, 160 108 Z" fill="#FFFBEB"/>';

const BACKGROUND = '<rect x="24" y="14" width="272" height="156" rx="22" fill="#1E293B"/>'
  + '<path d="M24 142 L68 102 L110 132 L162 92 L214 130 L256 104 L296 142 L296 170 L24 170 Z" fill="#283548"/>'
  + '<path d="M24 148 Q160 134 296 148 L296 170 L24 170 Z" fill="#1E2631"/>';

const DECOR = '<polygon points="52,145 78,108 104,145" fill="#3B7A57" stroke="#6EE7B7" stroke-width="1"/>'
  + '<polygon points="78,108 78,145 92,145" fill="#234E36"/>'
  + '<circle cx="255" cy="42" r="10" fill="#FEF3C7" opacity="0.85"/>'
  + '<circle cx="259" cy="39" r="9" fill="#1E293B"/>';

const FIREWOOD = '<g transform="translate(160, 146)">'
  + '<rect x="-24" y="-5" width="48" height="9" rx="4" transform="rotate(-14)" fill="#78350F" stroke="#92400E" stroke-width="1"/>'
  + '<rect x="-24" y="-5" width="48" height="9" rx="4" transform="rotate(14)" fill="#5C280B" stroke="#78350F" stroke-width="1"/>'
  + '<circle cx="-22" cy="4" r="4" fill="#475569"/>'
  + '<circle cx="0" cy="7" r="4.5" fill="#64748B"/>'
  + '<circle cx="22" cy="4" r="4" fill="#475569"/></g>';

const AURORA = '<defs><linearGradient id="auroraGrad" x1="0%" y1="0%" x2="100%" y2="0%">'
  + '<stop offset="0%" stop-color="#34D399" stop-opacity="0"/>'
  + '<stop offset="30%" stop-color="#34D399" stop-opacity="0.45"/>'
  + '<stop offset="70%" stop-color="#60A5FA" stop-opacity="0.45"/>'
  + '<stop offset="100%" stop-color="#A78BFA" stop-opacity="0"/></linearGradient></defs>'
  + '<path d="M35 45 Q100 20 160 42 T285 35" fill="none" stroke="url(#auroraGrad)" stroke-width="16" stroke-linecap="round"/>'
  + '<path d="M45 64 Q120 40 180 58 T275 50" fill="none" stroke="url(#auroraGrad)" stroke-width="9" stroke-linecap="round"/>'
  + '<circle cx="65" cy="34" r="1.8" fill="#FEF08A"/>'
  + '<circle cx="245" cy="28" r="2" fill="#FEF08A"/>'
  + '<circle cx="130" cy="24" r="1.5" fill="#FFF"/>'
  + '<circle cx="198" cy="30" r="1.5" fill="#FFF"/>';

const GLOW_DEFINITION = '<defs><radialGradient id="fireGlowGrad" cx="50%" cy="60%" r="50%">'
  + '<stop offset="0%" stop-color="#FB923C" stop-opacity="0.55"/>'
  + '<stop offset="55%" stop-color="#FDE047" stop-opacity="0.2"/>'
  + '<stop offset="100%" stop-color="#FFFFFF" stop-opacity="0"/></radialGradient></defs>';

// The generated SVGs contain only ASCII, so the same small base64 encoder used
// by class-index.js works in the mini-program without Buffer, btoa or a network.
function svgUrl(content, width = 320, height = 185, viewBox = VIEW_BOX) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="${viewBox}">${content}</svg>`;
  let encoded = '';
  for (let index = 0; index < svg.length; index += 3) {
    const a = svg.charCodeAt(index);
    const b = svg.charCodeAt(index + 1);
    const c = svg.charCodeAt(index + 2);
    const word = (a << 16) | ((b || 0) << 8) | (c || 0);
    encoded += BASE64_CHARS[(word >>> 18) & 63] + BASE64_CHARS[(word >>> 12) & 63]
      + (index + 1 < svg.length ? BASE64_CHARS[(word >>> 6) & 63] : '=')
      + (index + 2 < svg.length ? BASE64_CHARS[word & 63] : '=');
  }
  return `data:image/svg+xml;base64,${encoded}`;
}

const BACKGROUND_SRC = svgUrl(BACKGROUND);
const DECOR_SRC = svgUrl(DECOR);
const AURORA_SRC = svgUrl(AURORA);
const EMPTY_SRC = svgUrl('');

function numberInRange(value, fallback, minimum, maximum) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(minimum, Math.min(maximum, Math.floor(number))) : fallback;
}

function stageFor(count, total, percent) {
  if (count === 0) return { stageIdx: 0, title: '0阶段 · 营地待燃',
    subtitle: total > 0 ? '夜幕降临，等待第 1 位成员点亮夜读篝火...'
      : '夜幕降临，等待成员点亮夜读篝火...' };
  if (count === total) return { stageIdx: 4, title: '满阶 · 极光星夜达成',
    subtitle: `全员 ${total}/${total} 围炉夜读！夜空绽放壮丽极光 🌌` };
  if (percent < 40) return { stageIdx: 1, title: '1阶段 · 星火初燃',
    subtitle: `已有 ${count}/${total} 人点亮营灯 (${percent}%)，火苗初升 🔥` };
  if (percent < 75) return { stageIdx: 2, title: '2阶段 · 暖火映书',
    subtitle: `已有 ${count}/${total} 人围炉共学 (${percent}%)，篝火正旺 ⛺` };
  return { stageIdx: 3, title: '3阶段 · 繁星汇聚',
    subtitle: `已完成 ${count}/${total} 人，差 ${total - count} 人即可召唤全员极光 ✨` };
}

function lanterns(count, total) {
  // Current groups contain at most ten members. Keep the reference's single
  // arc through twelve; larger groups can use up to three spaced arcs without
  // cropping the scene or silently discarding members.
  const rowCount = Math.min(3, Math.ceil(total / 12));
  const rowSize = rowCount > 0 ? Math.ceil(total / rowCount) : 0;
  let result = '';
  for (let index = 0; index < total; index += 1) {
    const row = Math.floor(index / rowSize);
    const rowTotal = Math.min(rowSize, total - row * rowSize);
    const rowIndex = index - row * rowSize;
    const position = rowTotal === 1 ? 0.5 : rowIndex / (rowTotal - 1);
    const angle = Math.PI * 1.12 + position * (Math.PI * 0.76);
    const radiusX = rowCount === 1 ? 96 : 80 + row * 16;
    // Lift the single arc over the fully grown flame. Odd member counts have a
    // center lamp, which would otherwise cover the flame's white core.
    const radiusY = rowCount === 1 ? 83 : 30 + row * 19;
    const cx = (160 + radiusX * Math.cos(angle)).toFixed(1);
    const cy = (138 + radiusY * Math.sin(angle)).toFixed(1);
    const baseRadius = rowCount === 1 ? (total <= 6 ? 9 : 7) : 6;
    // The shortest step on the elliptical arc is near its ends. Shrink lamps
    // when an expanded membership makes a row denser, rather than overlap them.
    const step = rowTotal > 1 ? Math.PI * 0.76 / (rowTotal - 1) : 0;
    const nextAngle = Math.PI * 1.12 + step;
    const spacing = rowTotal > 1 ? Math.hypot(
      radiusX * (Math.cos(nextAngle) - Math.cos(Math.PI * 1.12)),
      radiusY * (Math.sin(nextAngle) - Math.sin(Math.PI * 1.12)),
    ) : Infinity;
    const radius = Math.min(baseRadius, spacing * 0.46);
    const iconHeight = rowCount === 1 && total <= 6 ? 8.5 : radius;
    const isChecked = index < count;
    result += '<g>';
    if (isChecked) result += `<circle cx="${cx}" cy="${cy}" r="${radius + 4}" fill="#FBBF24" opacity="0.35"/>`;
    result += `<circle cx="${cx}" cy="${cy}" r="${radius}" fill="${isChecked ? '#FEF08A' : '#334155'}" stroke="${isChecked ? '#F59E0B' : '#64748B'}" stroke-width="1.5"/>`;
    // Reuse the original fire paths as the lamp icon: SVG image rendering in
    // WeChat does not consistently provide the reference's emoji font.
    if (isChecked) result += `<g transform="translate(${cx},${(Number(cy) + 3).toFixed(1)}) scale(${(iconHeight / 72).toFixed(6)}) translate(-160,-144)">${FIRE_PATHS}</g>`;
    else result += `<text x="${cx}" y="${(Number(cy) + iconHeight * 0.4).toFixed(1)}" text-anchor="middle" font-size="${Math.min(iconHeight, radius * 1.8 / String(index + 1).length)}" font-family="sans-serif" fill="#94A3B8" font-weight="bold">${index + 1}</text>`;
    result += '</g>';
  }
  return result;
}

function progressRing(ratio) {
  const circumference = 2 * Math.PI * 42;
  const track = '<circle cx="50" cy="50" r="42" fill="none" stroke="#E6EAE2" stroke-width="8"/>';
  const progress = ratio > 0
    ? `<circle cx="50" cy="50" r="42" fill="none" stroke="#355F46" stroke-width="8" stroke-linecap="round" stroke-dasharray="${circumference.toFixed(6)}" stroke-dashoffset="${(circumference * (1 - ratio)).toFixed(6)}" transform="rotate(-90 50 50)"/>`
    : '';
  return svgUrl(track + progress, 100, 100, '0 0 100 100');
}

function renderScene(completed, memberTotal = DEFAULT_TOTAL) {
  const total = numberInRange(memberTotal, DEFAULT_TOTAL, 0, Number.MAX_SAFE_INTEGER);
  const count = numberInRange(completed, 0, 0, total);
  const ratio = total > 0 ? count / total : 0;
  const percent = Math.round(ratio * 100);
  const isFull = total > 0 && count === total;
  const fireScale = count === 0 ? 0 : 0.35 + ratio * 0.72;
  const glowScale = 0.2 + ratio * 1.05;
  const glowOpacity = 0.15 + ratio * 0.85;
  const fire = count === 0 ? ''
    : `<g transform="translate(160,144) scale(${fireScale.toFixed(6)}) translate(-160,-144)">${FIRE_PATHS}</g>`;
  const glow = `${GLOW_DEFINITION}<circle cx="160" cy="128" r="${(68 * glowScale).toFixed(6)}" fill="url(#fireGlowGrad)" opacity="${glowOpacity.toFixed(6)}"/>`;
  return Object.assign({
    backgroundSrc: BACKGROUND_SRC,
    auroraSrc: isFull ? AURORA_SRC : EMPTY_SRC,
    decorSrc: DECOR_SRC,
    glowSrc: svgUrl(glow),
    sceneSrc: svgUrl(lanterns(count, total) + FIREWOOD),
    fireSrc: svgUrl(fire),
    progressRingSrc: progressRing(ratio),
    count,
    total,
    percent,
    ratio,
    isFull,
    fireScale,
    glowScale,
    glowOpacity,
    auroraOpacity: isFull ? 1 : 0,
  }, stageFor(count, total, percent));
}

module.exports = { renderScene, DEFAULT_TOTAL };
