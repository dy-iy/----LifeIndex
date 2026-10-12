const session = require("./session");
const listeners = new Set();
let revision = 0;
function subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }
function publish(snapshot, detail = {}) {
  const current = session.identity();
  if (!current.owner || !snapshot || snapshot.owner !== current.owner) return;
  revision += 1;
  const change = { ...detail, owner: current.owner, generation: current.generation, revision };
  // A saved mutation stays successful even if a hidden page cannot refresh.
  listeners.forEach((listener) => { try { listener(change); } catch (_) {} });
}
module.exports = { subscribe, publish };
