const DURATION = 300;
const STAGGER = 60;
const SETTLE = DURATION + STAGGER + 20;

function positive(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}

function height(rect) {
  return positive(rect && rect.height, 0);
}

function pixels(value) {
  return String(Math.round(value * 1000) / 1000);
}

// SelectorQuery measures the natural inner content. Add only the outer border;
// every rpx distance is based on window width, independently of stage width.
function buildLayout(rects, coverRect, width, rpxRatio) {
  const measured = Array.isArray(rects) ? rects : [];
  const ratio = positive(rpxRatio, 0.5);
  const stageWidth = positive(width, 1);
  const foldedCardHeight = height(coverRect) + 2 * ratio;
  let openHeight = 0;
  const cards = measured.map((rect, index) => {
    const depth = Math.min(index, 2);
    const openCardHeight = height(rect) + 2 * ratio;
    const card = {
      openHeight: openCardHeight,
      foldHeight: foldedCardHeight,
      openY: openHeight,
      foldY: depth * 14 * ratio,
      foldScale: Math.max(0, (stageWidth - depth * 24 * ratio) / stageWidth),
      foldRotation: 0,
      foldOpacity: index < 3 ? 1 : 0,
      zIndex: measured.length - index,
      progress: measured.length > 1 ? index / (measured.length - 1) : 0
    };
    openHeight += openCardHeight + (index < measured.length - 1 ? 18 * ratio : 0);
    return card;
  });
  return {
    cards,
    openHeight,
    foldHeight: cards.length ? foldedCardHeight + Math.min(cards.length - 1, 2) * 14 * ratio : 0
  };
}

function frame(layout, open, moving) {
  const cards = layout.cards;
  return {
    courseDeckStyles: cards.map((card) => {
      const delay = moving && cards.length > 1
        ? (open ? card.progress : 1 - card.progress) * STAGGER : 0;
      return `height:${pixels(open ? card.openHeight : card.foldHeight)}px;`
        + `transform:translate3d(0,${pixels(open ? card.openY : card.foldY)}px,0) rotate(${pixels(open ? 0 : card.foldRotation)}deg) scaleX(${pixels(open ? 1 : card.foldScale)});`
        + `opacity:${open ? 1 : card.foldOpacity};z-index:${card.zIndex};transition-delay:${pixels(delay)}ms;`;
    }),
    courseDeckHeight: `height:${pixels(open ? layout.openHeight : layout.foldHeight)}px;`
  };
}

module.exports = { DURATION, STAGGER, SETTLE, buildLayout, frame };
