const backend = require("./backend");
const session = require("./session");
const drafts = require("./review-draft");
const metrics = require("./review-metrics");
const focus = require("./review-focus");
const QUESTION_VERSION = "class-review-v1";
const BRANCH_VERSION = "class-review-v2";
let identity = null;
let entries = new Map();
let reads = new Map();
let lists = new Map();
let sequence = 0;
const copy = (value) => JSON.parse(JSON.stringify(value));
const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function context() {
  const current = session.identity();
  if (!identity || identity.owner !== current.owner || identity.generation !== current.generation) {
    identity = current; entries = new Map(); reads = new Map(); lists = new Map();
  }
  if (!current.owner) throw new Error("请先登录后填写复盘。");
  return current;
}
function assertCurrent(snapshot) {
  if (!session.isCurrent(snapshot)) throw new Error("登录状态已变化，请重新进入复盘。");
}
function entry(id) {
  const snapshot = context();
  if (!entries.has(id)) {
    let stored, input;
    try { stored = drafts.read(snapshot.owner, id); input = drafts.readInput(snapshot.owner, id); }
    catch (_) { throw new Error("本机草稿暂时无法读取，请重试后再填写。"); }
    if (stored && stored.schema && stored.schema !== 2) throw new Error("本机草稿版本无法读取，请保留数据后更新小程序。");
    if (stored && stored.schema === 2 && (!Array.isArray(stored.answers) || ![1, 3].includes(stored.answers.length)
      || stored.answers.some((answer) => typeof answer !== "string")
      || (stored.pending && (!stored.pending.key || !stored.pending.body || !["save", "submit"].includes(stored.pending.operation))))) {
      throw new Error("本机草稿数据无法读取，请保留数据后重试。");
    }
    const value = stored && stored.schema === 2 ? stored : {
      schema: 2, answers: drafts.normalize(stored && stored.answers), baseVersion: null,
      dirty: Boolean(stored && drafts.normalize(stored.answers).some((answer) => answer.trim())),
      cloud: null, pending: null, conflict: false,
    };
    if (value.evaluationPending && (!value.evaluationPending.key || !value.evaluationPending.body
      || !["retry", "clarify", "revise", "finish", "note"].includes(value.evaluationPending.operation))) throw new Error("待确认的评估操作无法读取，请保留本机数据。");
    const restored = Object.assign({ evaluation: null, evaluationPending: null, revision: false, clarificationAnswer: "",
      clarificationParticipation: "unspecified", reviewContext: {}, questionVersion: value.cloud && value.cloud.question_version || QUESTION_VERSION,
      inputRevision: 0, noteDraft: null }, copy(value));
    if (input && (!Number.isSafeInteger(input.inputRevision) || input.inputRevision < 1)) throw new Error("本机输入日志无法读取，请保留数据后重试。");
    if (input && input.inputRevision > restored.inputRevision) {
      if (!Array.isArray(input.answers) || ![1, 3].includes(input.answers.length) || input.answers.some((a) => typeof a !== "string")) throw new Error("本机输入无法读取，请保留数据后重试。");
      Object.assign(restored, input);
    }
    if (![QUESTION_VERSION, BRANCH_VERSION].includes(restored.questionVersion)
      || restored.answers.length !== (restored.questionVersion === BRANCH_VERSION ? 1 : 3)) throw new Error("本机草稿的问题版本与答案不一致，请保留数据后重试。");
    if (restored.cloud) delete restored.cloud.evaluation;
    entries.set(id, Object.assign(restored, { storageError: "", flight: null, evaluationFlight: null }));
  }
  return entries.get(id);
}
function persistInput(id, value) {
  const started = metrics.now();
  const snapshot = context();
  value.inputRevision += 1;
  const input = Object.fromEntries(["inputRevision", "answers", "baseVersion", "dirty", "revision", "questionVersion", "reviewContext",
    "clarificationAnswer", "clarificationParticipation", "noteDraft"].map((key) => [key, value[key]]));
  try { drafts.writeInput(snapshot.owner, id, input); value.storageError = ""; }
  catch (_) { value.storageError = "草稿未能保存到本机，请重试后再离开页面。"; }
  metrics.record("input_storage", started);
}
function inputState(value) {
  return { answers: value.answers.slice(), dirty: value.dirty, storageError: value.storageError,
    clarificationAnswer: value.clarificationAnswer, clarificationParticipation: value.clarificationParticipation,
    noteDraft: value.noteDraft };
}
function sameInput(value, remote) {
  return equal(value.answers, remote.answers) && value.questionVersion === remote.question_version
    && equal(value.reviewContext || {}, remote.review_context || {});
}
function acceptInput(value, remote) {
  value.answers = remote.answers.slice(); value.questionVersion = remote.question_version || QUESTION_VERSION;
  value.reviewContext = copy(remote.review_context || {});
}
function bodyFor(value) {
  const body = { version: value.baseVersion, question_version: value.questionVersion, answers: value.answers.slice() };
  if (value.questionVersion === BRANCH_VERSION) body.review_context = value.reviewContext;
  return body;
}
function readOnce(key, action) {
  if (reads.has(key)) return reads.get(key);
  const map = reads;
  const promise = action().finally(() => { if (map.get(key) === promise) map.delete(key); });
  map.set(key, promise); return promise;
}
function persist(id, value, critical = false) {
  const snapshot = context();
  const { flight, evaluationFlight, storageError, ...stored } = value;
  try { drafts.write(snapshot.owner, id, stored); value.storageError = ""; }
  catch (_) {
    value.storageError = "草稿未能保存到本机，请重试后再离开页面。";
    if (critical) throw new Error(value.storageError);
  }
}
function state(id) {
  const { flight, evaluationFlight, ...value } = entry(id);
  return copy(value);
}
function ingest(remote) {
  const id = remote.session.id;
  const value = entry(id);
  // A slow list/read must not replace a newer acknowledged write.
  if (value.cloud && (value.cloud.version > remote.version || value.cloud.version === remote.version && (value.cloud.note_version || 0) > (remote.note_version || 0))) return state(id);
  const { evaluation: embeddedEvaluation, ...cloud } = remote;
  value.cloud = { ...copy(cloud), evaluation_loaded: Boolean(embeddedEvaluation || cloud.evaluation_loaded) };
  if (remote.evaluation) {
    const old = value.evaluation && value.evaluation.evaluation, next = remote.evaluation.evaluation;
    if (!old || next && (next.review_version > old.review_version || next.review_version === old.review_version && next.version >= old.version)) value.evaluation = copy(remote.evaluation);
  }
  if (!value.pending && !value.evaluationPending) {
    if (!value.dirty || sameInput(value, remote)) {
      acceptInput(value, remote); value.baseVersion = remote.version;
      value.dirty = false; value.conflict = false;
    } else if ((value.baseVersion === null && remote.version === 0) || value.baseVersion === remote.version) {
      value.baseVersion = remote.version; value.conflict = false;
    } else value.conflict = true;
    if (remote.status === "pending_evaluation" && value.dirty && !value.revision) value.conflict = true;
  }
  persist(id, value);
  return state(id);
}
async function list(status = "all", cursor = null) {
  const snapshot = context();
  return readOnce(`list:${status}:${cursor || ""}`, async () => {
    const started = metrics.now();
    const result = await backend.request({ path: `/api/reviews?status=${encodeURIComponent(status)}&compact=true${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}` });
    metrics.record("history_request", started);
    assertCurrent(snapshot);
    const previous = lists.get(status);
    const combined = cursor && previous ? { items: [...previous.items, ...result.items.filter((item) => !previous.items.some((old) => old.session.id === item.session.id))], next_cursor: result.next_cursor } : result;
    lists.set(status, copy(combined));
    try { wx.setStorageSync(`lifefund.review-list:${encodeURIComponent(snapshot.owner)}:${status}`, copy(combined)); } catch (_) { /* Optional list cache; editable drafts report their own failures. */ }
    for (const remote of result.items) if (Array.isArray(remote.answers)) {
      try { ingest(remote); } catch (_) { /* The selected record renders its own recoverable draft error. */ }
    }
    return result;
  });
}
async function load(id) {
  const snapshot = context();
  entry(id); // Do not overwrite an unreadable local draft with a cloud response.
  return readOnce(`review:${id}`, async () => {
    const started = metrics.now();
    const remote = await backend.request({ path: `/api/reviews/${encodeURIComponent(id)}` });
    metrics.record("detail_request", started);
    assertCurrent(snapshot); return ingest(remote);
  });
}
function edit(id, answers) {
  const value = entry(id);
  if (value.pending || value.flight || value.evaluationPending || value.evaluationFlight
    || (value.cloud && (value.cloud.voided || value.cloud.status === "pending_evaluation" && !value.revision))) return state(id);
  value.answers = value.questionVersion === BRANCH_VERSION ? [String(answers[0] || "").slice(0, 8000)] : drafts.normalize(answers);
  value.dirty = !value.cloud || !sameInput(value, value.cloud);
  persistInput(id, value);
  return inputState(value);
}
function retryLocal(id) { const value = entry(id); persist(id, value); return state(id); }
function resolve(id, source) {
  const value = entry(id);
  if (!value.cloud || value.pending || value.flight || value.evaluationPending || value.evaluationFlight) throw new Error("请先刷新云端记录或重试未确认的操作。");
  if (source === "local" && value.cloud.status !== "draft" && !value.revision) throw new Error("云端已提交，不能覆盖已提交答案。");
  if (!["local", "cloud"].includes(source)) throw new Error("请选择要保留的答案。");
  if (source === "cloud") { acceptInput(value, value.cloud); value.revision = false; }
  value.baseVersion = value.cloud.version;
  value.dirty = !sameInput(value, value.cloud);
  value.conflict = false;
  persist(id, value);
  return state(id);
}
async function send(id, value, snapshot) {
  const pending = copy(value.pending);
  try {
    const remote = await backend.request({
      path: `/api/reviews/${encodeURIComponent(id)}${pending.operation === "submit" ? "/submit" : ""}`,
      method: pending.operation === "submit" ? "POST" : "PUT", data: pending.body,
      header: { "Idempotency-Key": pending.key },
    });
    assertCurrent(snapshot);
    value.pending = null; acceptInput(value, remote); value.dirty = false;
    return ingest(value.cloud && value.cloud.version > remote.version ? value.cloud : remote);
  } catch (error) {
    assertCurrent(snapshot);
    if ([400, 401, 403, 404, 409, 422].includes(error.statusCode)) {
      value.pending = null;
      persist(id, value);
      if (error.statusCode === 409) {
        value.conflict = true;
        try { await load(id); } catch (_) { /* Keep input; refresh remains available. */ }
      }
    }
    throw error;
  }
}
function write(id, operation) {
  const snapshot = context();
  const value = entry(id);
  if (value.evaluationPending || value.evaluationFlight) throw new Error("请先重试并确认上次评估操作。");
  if (value.flight) return value.flight;
  if (!value.pending) {
    if (operation === "retry") throw new Error("没有待确认的操作，请刷新云端状态。");
    if (!value.cloud || value.baseVersion === null) throw new Error("请先成功读取云端草稿后再保存。");
    if (value.conflict) throw new Error("请先核对两份答案并选择保留哪一份。");
    if (value.cloud.status !== "draft") throw new Error("复盘已提交，当前不支持修改。");
    if (operation === "submit" && value.questionVersion !== BRANCH_VERSION && value.answers.some((answer) => !answer.trim())) throw new Error("请填写全部三项复盘后再提交。");
    value.pending = { operation, key: `review-${Date.now()}-${++sequence}-${Math.random().toString(36).slice(2)}`,
      body: bodyFor(value) };
    try { persist(id, value, true); }
    catch (error) { value.pending = null; throw error; }
  }
  // Replay the persisted operation and payload, even after process restart.
  value.flight = send(id, value, snapshot).finally(() => { value.flight = null; });
  return value.flight;
}
function ingestEvaluation(id, remote) {
  const value = entry(id);
  const old = value.evaluation && value.evaluation.evaluation, next = remote && remote.evaluation;
  if (old && (!next || old.review_version > next.review_version || (old.review_version === next.review_version && old.version > next.version))) return state(id);
  if (remote.review && remote.review.session.id === id) ingest(remote.review);
  value.evaluation = copy(remote);
  if (next && value.cloud && (next.result_review_version || next.review_version) === value.cloud.version) {
    value.cloud.evaluation_summary = { id: next.id, status: next.status, version: next.version,
      review_version: next.review_version, result_kind: next.result_kind };
  }
  persist(id, value);
  return state(id);
}
async function loadEvaluation(id) {
  const snapshot = context(); entry(id);
  return readOnce(`evaluation:${id}`, async () => {
    const remote = await backend.request({ path: `/api/reviews/${encodeURIComponent(id)}/evaluation` });
    assertCurrent(snapshot); return ingestEvaluation(id, remote);
  });
}
function beginRevision(id) {
  const value = entry(id);
  if (!value.cloud || !value.evaluation || !value.evaluation.can_revise) throw new Error("当前记录无法修改，请刷新确认纠错期限。");
  const evaluation = value.evaluation.evaluation;
  if (!evaluation || evaluation.status !== "invalid_answer"
    || evaluation.review_version !== value.cloud.version) throw new Error("已提交答案仅供回看，只有无效回答可以重新填写。");
  if (value.pending || value.evaluationPending || value.flight || value.evaluationFlight || value.conflict) throw new Error("请先确认待处理操作或核对答案冲突。");
  value.revision = true;
  persist(id, value); return state(id);
}
function editClarification(id, answer) {
  const value = entry(id);
  if (value.evaluationPending || value.evaluationFlight) return state(id);
  value.clarificationAnswer = typeof answer === "string" ? answer.slice(0, 2000) : "";
  persistInput(id, value); return inputState(value);
}
async function sendEvaluation(id, value, snapshot) {
  const pending = copy(value.evaluationPending);
  try {
    const remote = await backend.request({ path: `/api/reviews/${encodeURIComponent(id)}${pending.operation === "note" ? "/note" : `/evaluation/${pending.operation}`}`,
      method: pending.operation === "note" ? "PATCH" : "POST", data: pending.body, header: { "Idempotency-Key": pending.key } });
    assertCurrent(snapshot);
    if (pending.operation === "revise" && (!remote.review || !Array.isArray(remote.review.answers))) throw new Error("修订结果尚未确认，请重试同一次操作。");
    value.evaluationPending = null;
    if (pending.operation === "note") { value.noteDraft = null; return ingest(remote); }
    if (pending.operation === "clarify") { value.clarificationAnswer = ""; value.clarificationParticipation = "unspecified"; }
    if (pending.operation === "revise") {
      value.revision = false; value.dirty = false; value.conflict = false;
      acceptInput(value, remote.review);
      ingest(value.cloud && value.cloud.version > remote.review.version ? value.cloud : remote.review);
    }
    ingestEvaluation(id, remote);
    return state(id);
  } catch (error) {
    assertCurrent(snapshot);
    if ([400, 401, 403, 404, 409, 422].includes(error.statusCode)) {
      value.evaluationPending = null; persist(id, value);
      if (error.statusCode === 409) {
        await Promise.allSettled([load(id), loadEvaluation(id)]);
      }
    }
    throw error;
  }
}
function writeEvaluation(id, operation) {
  const snapshot = context(), value = entry(id);
  if (value.evaluationFlight) return value.evaluationFlight;
  if (value.pending || value.flight) throw new Error("请先确认复盘保存或提交结果。");
  if (!value.evaluationPending) {
    const evaluation = value.evaluation && value.evaluation.evaluation;
    if (!["retry", "clarify", "revise", "finish"].includes(operation)) throw new Error("没有待确认的评估操作。");
    if (!evaluation) throw new Error("请先刷新评估状态。");
    let body = { version: evaluation && evaluation.version };
    if (operation === "revise") {
      if (evaluation.status !== "invalid_answer" || evaluation.review_version !== value.cloud.version)
        throw new Error("已提交答案仅供回看，只有无效回答可以重新填写。");
      if (!value.revision || value.conflict || !value.evaluation.can_revise) throw new Error("请先核对答案和纠错期限。");
      if (!value.dirty) throw new Error("答案尚未修改，无需重复评估。");
      if (value.questionVersion !== BRANCH_VERSION && value.answers.some((answer) => !answer.trim())) throw new Error("请填写全部三项复盘后再提交。");
      body = bodyFor(value);
    } else if (operation === "clarify") {
      if (evaluation.flow && evaluation.flow.key === "focus" && focus.choice(value.clarificationAnswer) === "unspecified") throw new Error("请选择本次专注情况。");
      if (evaluation.status !== "needs_input" || !value.clarificationAnswer.trim() && value.clarificationParticipation === "unspecified") throw new Error("请填写补充说明后再提交。");
      body.answer = value.clarificationAnswer;
      if (value.questionVersion === BRANCH_VERSION) body.participation = value.clarificationParticipation;
    } else if (operation !== "finish" && evaluation.status !== "failed") throw new Error("当前评估无需重试，请刷新状态。");
    value.evaluationPending = { operation, body, key: `evaluation-${Date.now()}-${++sequence}-${Math.random().toString(36).slice(2)}` };
    try { persist(id, value, true); } catch (error) { value.evaluationPending = null; throw error; }
  }
  value.evaluationFlight = sendEvaluation(id, value, snapshot).finally(() => { value.evaluationFlight = null; });
  return value.evaluationFlight;
}
function editContext(id, patch) {
  const value = entry(id);
  if (value.questionVersion !== BRANCH_VERSION || value.pending || value.evaluationPending || value.flight || value.evaluationFlight
    || value.cloud && value.cloud.status !== "draft" && !value.revision) return state(id);
  value.reviewContext = { ...value.reviewContext, ...patch };
  value.dirty = !value.cloud || !sameInput(value, value.cloud);
  persistInput(id, value); return state(id);
}
function upgrade(id) {
  const value = entry(id);
  if (value.pending || value.evaluationPending || value.cloud && value.cloud.status !== "draft" && !value.revision) return state(id);
  if (value.questionVersion !== BRANCH_VERSION) {
    value.answers = [value.answers.map((text, i) => text ? `${["学习内容", "理解或疑问", "专注情况"][i]}：${text}` : "").filter(Boolean).join("\n")];
    value.questionVersion = BRANCH_VERSION; value.reviewContext = {}; value.dirty = true; persistInput(id, value);
  }
  return state(id);
}
function editChoice(id, choice) {
  const value = entry(id);
  if (value.evaluationPending || value.evaluationFlight) return state(id);
  value.clarificationParticipation = choice; persistInput(id, value); return state(id);
}
function cachedList(filter) {
  const snapshot = context();
  if (!lists.has(filter)) {
    try { const value = wx.getStorageSync(`lifefund.review-list:${encodeURIComponent(snapshot.owner)}:${filter}`); if (value && Array.isArray(value.items)) lists.set(filter, value); }
    catch (_) { /* Read-only list cache is optional. */ }
  }
  return lists.has(filter) ? copy(lists.get(filter)) : null;
}
module.exports = { state, ingest, list, load, edit, retryLocal, resolve, write,
  loadEvaluation, ingestEvaluation, beginRevision, editClarification, writeEvaluation,
  editContext, upgrade, editChoice, cachedList, BRANCH_VERSION };
