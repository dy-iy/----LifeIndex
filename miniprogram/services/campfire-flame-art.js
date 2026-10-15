// Native SVG artwork. Animate the separate images in WXSS, not inside the SVG.
// All fire shapes share the fixed (160, 144) base and use ASCII-only markup.
const FLAME_DEFINITIONS = `<defs>
  <linearGradient id="campfireFlameOuter" gradientUnits="userSpaceOnUse" x1="157" y1="147" x2="169" y2="80">
    <stop stop-color="#FFCF55"/><stop offset=".24" stop-color="#FF9B22"/>
    <stop offset=".58" stop-color="#F85E0D"/><stop offset=".86" stop-color="#F34208" stop-opacity=".9"/>
    <stop offset="1" stop-color="#FF962E" stop-opacity=".78"/>
  </linearGradient>
  <linearGradient id="campfireFlameVeil" gradientUnits="userSpaceOnUse" x1="160" y1="146" x2="162" y2="91">
    <stop stop-color="#FF9D24" stop-opacity=".48"/><stop offset=".55" stop-color="#F84A0A" stop-opacity=".32"/>
    <stop offset="1" stop-color="#EF4510" stop-opacity="0"/>
  </linearGradient>
  <linearGradient id="campfireFlameGold" gradientUnits="userSpaceOnUse" x1="160" y1="145" x2="162" y2="97">
    <stop stop-color="#FFF8B7"/><stop offset=".34" stop-color="#FFE66B"/>
    <stop offset=".76" stop-color="#FFB72E"/><stop offset="1" stop-color="#FFA32B" stop-opacity=".45"/>
  </linearGradient>
  <linearGradient id="campfireFlameCore" gradientUnits="userSpaceOnUse" x1="160" y1="145" x2="164" y2="112">
    <stop stop-color="#FFFFFF"/><stop offset=".3" stop-color="#FFFDE7"/>
    <stop offset=".72" stop-color="#FFF4A1"/><stop offset="1" stop-color="#FFE17A" stop-opacity=".5"/>
  </linearGradient>
  <radialGradient id="campfireFlameBase" cx="50%" cy="75%" r="65%">
    <stop stop-color="#FFFDE0" stop-opacity=".9"/><stop offset=".5" stop-color="#FFD260" stop-opacity=".38"/>
    <stop offset="1" stop-color="#FFAD32" stop-opacity="0"/>
  </radialGradient>
</defs>`;

const OUTER_FLAMES = `<path d="M157 148 C141 148 132 140 134 129 C135 121 140 114 138 106 C148 110 151 116 151 120 C148 108 153 99 161 92 C167 87 169 82 167 78 C179 88 183 99 178 111 C188 110 189 102 188 98 C198 116 188 135 181 141 C175 147 167 149 157 148Z" fill="url(#campfireFlameVeil)"/>
  <path d="M159 146 C146 147 136 140 136 130 C135 120 143 115 140 104 C149 109 150 115 150 119 C148 110 150 102 158 94 C163 88 165 84 164 80 C174 87 178 97 174 107 C172 112 172 117 174 120 C178 116 183 111 182 102 C191 114 188 126 183 132 C180 142 171 147 159 146Z" fill="url(#campfireFlameOuter)"/>
  <path d="M147 140 C138 132 144 124 145 119 C145 128 153 130 152 138Z" fill="#FFBC38" opacity=".6"/>
  <path d="M170 141 C177 129 181 125 183 117 C185 129 180 138 174 142Z" fill="#FFDB68" opacity=".4"/>
  <path d="M160 138 C151 128 156 121 162 114 C169 106 168 101 167 97 C176 111 163 122 166 132Z" fill="url(#campfireFlameGold)" opacity=".48"/>`;

const INNER_FLAMES = `<path d="M157 145 C148 143 143 137 146 130 C148 124 153 119 152 112 C159 116 160 124 159 127 C164 121 169 116 168 106 C180 117 180 131 173 140 C169 145 163 147 157 145Z" fill="url(#campfireFlameGold)"/>
  <path d="M160 145 C153 143 151 138 154 131 C156 126 161 122 160 115 C168 121 171 126 169 133 C172 130 174 126 174 122 C180 134 171 145 164 146Z" fill="url(#campfireFlameCore)"/>
  <path d="M160 144 C156 140 159 136 162 132 C165 128 165 125 164 123 C171 131 168 140 164 144Z" fill="#FFFFF5" opacity=".88"/>
  <ellipse cx="160" cy="143" rx="19" ry="5" fill="url(#campfireFlameBase)"/>`;

const SPARKS_BACK = `<g fill="#FFB954">
  <ellipse cx="144" cy="116" rx=".65" ry="1.8" transform="rotate(-18 144 116)" opacity=".75"/>
  <ellipse cx="177" cy="91" rx=".6" ry="1.5" transform="rotate(21 177 91)" opacity=".65"/>
  <ellipse cx="153" cy="98" rx=".5" ry="1.2" opacity=".5"/>
  <circle cx="180" cy="123" r=".75" opacity=".75"/>
  <circle cx="150" cy="130" r=".6" opacity=".6"/>
</g>`;
const SPARKS_FRONT = `<g fill="#FFE19A">
  <ellipse cx="173" cy="79" rx=".5" ry="1.25" transform="rotate(18 173 79)" opacity=".55"/>
  <ellipse cx="148" cy="106" rx=".55" ry="1.65" transform="rotate(-24 148 106)" opacity=".85"/>
  <ellipse cx="171" cy="122" rx=".65" ry="1.65" opacity=".85"/>
  <circle cx="163" cy="102" r=".55" opacity=".45"/>
</g>`;

function renderFireGlow(scale, opacity) {
  const radiusX = (57 * scale).toFixed(3);
  const radiusY = (48 * scale).toFixed(3);
  return `<defs>
    <radialGradient id="campfireAtmosphere"><stop stop-color="#FFA62D" stop-opacity=".26"/><stop offset=".48" stop-color="#F99B29" stop-opacity=".09"/><stop offset="1" stop-color="#FF8B24" stop-opacity="0"/></radialGradient>
    <radialGradient id="campfireGroundLight"><stop stop-color="#FFB54A" stop-opacity=".28"/><stop offset=".5" stop-color="#FF9429" stop-opacity=".12"/><stop offset="1" stop-color="#FF7E20" stop-opacity="0"/></radialGradient>
    <clipPath id="campfireGlowBounds"><rect x="24" y="14" width="272" height="156" rx="22"/></clipPath>
  </defs><g clip-path="url(#campfireGlowBounds)" opacity="${opacity.toFixed(4)}">
    <ellipse cx="160" cy="126" rx="${radiusX}" ry="${radiusY}" fill="url(#campfireAtmosphere)"/>
    <ellipse cx="160" cy="152" rx="${(52 * scale).toFixed(3)}" ry="${(13 * scale).toFixed(3)}" fill="url(#campfireGroundLight)"/>
  </g>`;
}

module.exports = { FLAME_DEFINITIONS, OUTER_FLAMES, INNER_FLAMES, SPARKS_BACK, SPARKS_FRONT, renderFireGlow };
