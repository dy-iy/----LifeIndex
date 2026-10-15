// Fine SVG details for the 320 x 185 campfire scene. Keep this fragment ASCII:
// campfire-scene encodes SVG without a UTF-8 conversion step.
const DEFINITIONS = '<defs>'
  + '<linearGradient id="campfireWoodBack" x1="0" y1="0" x2="0" y2="1" gradientUnits="objectBoundingBox">'
  + '<stop offset="0" stop-color="#82543A"/><stop offset="0.27" stop-color="#5D3928"/>'
  + '<stop offset="0.67" stop-color="#36251F"/><stop offset="1" stop-color="#201C1A"/></linearGradient>'
  + '<linearGradient id="campfireWoodFront" x1="0" y1="0" x2="0" y2="1" gradientUnits="objectBoundingBox">'
  + '<stop offset="0" stop-color="#976244"/><stop offset="0.19" stop-color="#70452F"/>'
  + '<stop offset="0.57" stop-color="#4B2D22"/><stop offset="1" stop-color="#241C19"/></linearGradient>'
  + '<radialGradient id="campfireWoodCut" cx="41%" cy="34%" r="70%">'
  + '<stop offset="0" stop-color="#A47C53"/><stop offset="0.65" stop-color="#765338"/>'
  + '<stop offset="1" stop-color="#4B3326"/></radialGradient>'
  + '<linearGradient id="campfireWoodHeat" x1="0" y1="0" x2="0" y2="1" gradientUnits="objectBoundingBox">'
  + '<stop offset="0" stop-color="#FFC276" stop-opacity="0.87"/>'
  + '<stop offset="0.4" stop-color="#E98639" stop-opacity="0.35"/>'
  + '<stop offset="1" stop-color="#C45923" stop-opacity="0"/></linearGradient>'
  + '<radialGradient id="campfireWoodEmber" cx="50%" cy="35%" r="65%">'
  + '<stop offset="0" stop-color="#FFD18B"/><stop offset="0.35" stop-color="#EF843E"/>'
  + '<stop offset="0.76" stop-color="#9D3526"/><stop offset="1" stop-color="#4C2420"/></radialGradient>'
  + '</defs>';

const GROUND = '<ellipse cx="160" cy="157.3" rx="32" ry="4.8" fill="#10151A" opacity="0.3"/>'
  + '<g stroke="#211E1C" stroke-width="0.4" stroke-linejoin="round">'
  + '<path d="M137 154.1 L140.2 151.9 L145.8 152.6 L148.7 156.4 L145.1 159.5 L138.9 158.3 Z" fill="#34302B"/>'
  + '<path d="M144.1 156.5 L147.7 153.2 L152.8 154.4 L155 159 L150.4 160.9 L145.8 159.9 Z" fill="#2B2724"/>'
  + '<path d="M154.7 155.2 L160.3 153.8 L165.4 157.4 L164.1 161.6 L157.7 161.1 L153.8 158.5 Z" fill="#302A25"/>'
  + '<path d="M164.2 155.6 L170 153.7 L174.9 156.2 L175.4 159.8 L169.2 161 L164.1 159.4 Z" fill="#242321"/>'
  + '<path d="M176.3 153.5 L182.2 152.9 L186.3 156.2 L184.1 159.3 L177.2 159.4 L174.7 156.8 Z" fill="#35302B"/>'
  + '</g>'
  + '<path d="M138.6 154.4 L141 153.4 L144.1 154 M158.3 155.5 L160.4 155.1 L163 157.2 M178.7 154.5 L181.6 154.3 L183.4 155.7" fill="none" stroke="#736451" stroke-width="0.6" opacity="0.35" stroke-linecap="round"/>';

const BACK_LOG = '<g transform="translate(160,147) rotate(-13)">'
  + '<path d="M-23.8-4.1 C-13-4.4-5-3.8 4-4.2 L22.9-4 C26.2-3.9 27 2.7 23.6 4 L-23.6 4.2 C-27.2 3.4-27.3-3.5-23.8-4.1Z" fill="url(#campfireWoodBack)" stroke="#33231D" stroke-width="0.65"/>'
  + '<path d="M-22.5-2.9 C-14-3.5-4.5-2.7 2.9-3.2 S16.6-3.1 23-3" fill="none" stroke="#B18A61" stroke-width="0.65" opacity="0.46"/>'
  + '<path d="M-19-1.1 L-13.1-0.6 L-9-1.2 M-5.7 1 L2.4 0.4 L8.6 1 M12.6-1.2 L18.4-0.6 L23-1.3 M-19.8 2.8 L-14.1 2.2 M10.1 2.6 L15.5 2.8" fill="none" stroke="#1B1816" stroke-width="0.75" stroke-linecap="round" opacity="0.75"/>'
  + '<path d="M-11.4-3.9 L-10.5-2 L-11.7-0.8 M6.4-3.9 L5.6-2.4 L7-1.2 M19.5 1.1 L18.4 2.7 L19 3.8" fill="none" stroke="#2C211C" stroke-width="0.65"/>'
  + '<ellipse cx="-23.7" cy="0" rx="3.4" ry="4.3" fill="#352820"/>'
  + '<ellipse cx="-24.1" cy="-0.2" rx="2.65" ry="3.45" fill="url(#campfireWoodCut)"/>'
  + '<path d="M-24.6-2.6 C-26.6-1.6-26.3 2.5-24 2.7 C-21.4 2.5-21.4-2.6-24.6-2.6Z M-24.3-1.6 C-25.7-1.1-25.5 1.6-24 1.7 C-22.5 1.6-22.6-1.6-24.3-1.6Z" fill="none" stroke="#493324" stroke-width="0.42" opacity="0.8"/>'
  + '<path d="M-24.6 0.3 L-24.1-0.3 L-23.6 0.2 M-22.1 2.1 L-23.3 1.1" fill="none" stroke="#3B2A21" stroke-width="0.45" stroke-linecap="round"/>'
  + '</g>';

const FRONT_LOG = '<g transform="translate(160,147) rotate(13)">'
  + '<path d="M-24-4 C-27.9-2.6-27.3 3.3-23.6 4.4 L23.3 4.1 C26.8 3.8 27.3-3.1 23.5-4.3 L8.2-4 L-7.3-4.4Z" fill="url(#campfireWoodFront)" stroke="#35231B" stroke-width="0.7"/>'
  + '<path d="M-24.1-2.8 C-17.6-3.7-10.1-2.6-4.6-3 L7.2-2.8 L21.6-3.2" fill="none" stroke="#C3986A" stroke-width="0.75" stroke-linecap="round" opacity="0.5"/>'
  + '<path d="M-24 0.4 L-17.9-0.1 L-12.8 0.5 M-14.5 2.5 L-7.8 2.1 L-4.1 2.6 M-2.2-0.6 L5.8-0.1 L9.8-0.6 M10.8 2.1 L16.1 1.4 L21.9 2.2" fill="none" stroke="#211916" stroke-width="0.85" stroke-linecap="round" opacity="0.85"/>'
  + '<path d="M-20.2-2.3 L-15.8-1.6 L-11.8-1.9 M-5.8 1.1 L-1.1 0.9 M7.8-2 L13.8-1.4 L18.2-1.7" fill="none" stroke="#AF7950" stroke-width="0.5" stroke-linecap="round" opacity="0.35"/>'
  + '<path d="M-16-4 L-15.1-2.7 L-15.8-0.9 M-6.2 4 L-5.1 2.9 L-5.8 1.5 M2.1-3.9 L3.2-2.5 L2.6-1.3 M15.9 4 L14.9 2.6 L15.7 1.2" fill="none" stroke="#221A16" stroke-width="0.7"/>'
  + '<ellipse cx="23.5" cy="-0.05" rx="3.5" ry="4.5" fill="#39271E"/>'
  + '<ellipse cx="23.8" cy="-0.25" rx="2.8" ry="3.65" fill="url(#campfireWoodCut)"/>'
  + '<path d="M23.5-3 C21-2.5 20.9 2.2 23.2 2.9 C26.5 3.4 26.9-3.3 23.5-3Z M23.7-2 C22.1-1.9 21.9 1.4 23.4 1.9 C25.6 2.2 25.8-2.2 23.7-2Z M23.6-0.8 C22.7-0.4 23.1 0.9 23.8 0.7 C24.6 0.5 24.6-0.9 23.6-0.8Z" fill="none" stroke="#493122" stroke-width="0.43" opacity="0.85"/>'
  + '<path d="M25.7 2 L24.8 0.9 M23.2-3.3 L23.5-2.1" fill="none" stroke="#35241B" stroke-width="0.6" stroke-linecap="round"/>'
  + '</g>';

function renderFirewood(ratio) {
  const numericRatio = Number(ratio);
  const heat = Number.isFinite(numericRatio) ? Math.max(0, Math.min(1, numericRatio)) : 0;
  const heatOpacity = (heat * 0.64).toFixed(3);
  const emberOpacity = (heat * 0.82).toFixed(3);
  const litWood = heat > 0
    ? '<g opacity="' + heatOpacity + '">'
      + '<path d="M138.1 148.2 L180.8 138.6 Q183.8 138.4 184.9 141.2 L140.3 151.4Z" fill="url(#campfireWoodHeat)"/>'
      + '<path d="M137.5 138.6 L181.3 148.7 Q183.9 149.4 183.7 152.4 L136.1 141.7Z" fill="url(#campfireWoodHeat)"/>'
      + '<path d="M146.7 142 L155.2 143.7 M166 146.2 L174.1 147.7" fill="none" stroke="#F6B565" stroke-width="0.65" stroke-linecap="round"/>'
      + '</g>'
    : '';
  const embers = heat > 0
    ? '<g opacity="' + emberOpacity + '">'
      + '<path d="M148 153.8 L153.5 154.8 L156.8 157.6 L153.4 159.6 L148.9 158.7Z" fill="url(#campfireWoodEmber)"/>'
      + '<path d="M162.2 154.9 L168 155.1 L170.6 158.1 L167.4 159.8 L162.8 158.4Z" fill="url(#campfireWoodEmber)"/>'
      + '<path d="M176 152.9 L180 154.5 L179.2 157 L175.7 157.6 L173.5 155.7Z" fill="url(#campfireWoodEmber)"/>'
      + '<path d="M149.3 154.7 L151.8 156 L151.3 158 M164.1 155.9 L166.2 157.3 L167.8 157.1 M176 154.3 L177.3 155.3 L176.4 156.4" fill="none" stroke="#FFD28B" stroke-width="0.55" stroke-linecap="round"/>'
      + '<path d="M148.7 157.1 L150.9 156.2 L153.8 156.9 M164.6 158.9 L165 157.2 L166.6 155.9 M178.9 154.4 L177.3 155.3" fill="none" stroke="#46241D" stroke-width="0.8"/>'
      + '</g>'
    : '';
  return DEFINITIONS + GROUND + embers + BACK_LOG + FRONT_LOG + litWood;
}

module.exports = { renderFirewood };
