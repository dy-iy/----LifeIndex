const art = require("./art");
const session = require("./session");
const timer = require("./timer");
const reviews = require("./reviews");
const classIndex = require("./class-index");
const metrics = require("./review-metrics");
const focus = require("./review-focus");
const scoring = require("./review-scoring");
const reviewSignal = require("./review-signal");
const { displayError } = require("./backend");
const { PARTICIPATION_OPTIONS, UNTIMED_REASONS, LEGACY_QUESTIONS, untimedReasonLabel, displayRecord } = require("./review-presentation");
const EVALUATION_LABELS = { queued: "正在评估", running: "正在评估", needs_input: "已保存 · 可补充或结束", hold: "已保存 · 评分暂缓",
  failed: "评估失败", succeeded: "已结算", superseded: "已被新版本替代", invalid_answer: "待修改" };
const TEXT_STEPS = {
  learning: { title: "简述这节课学到了什么", placeholder: "比如：学会了链表的插入和删除，递归还没完全听懂……", incomplete: "请简述这节课学到了什么" },
  activity: { title: "这节课在做什么", placeholder: "比如：打游戏、聊天、做其他作业、发呆……", incomplete: "请简述这节课在做什么" },
  absence: { title: "未到课原因", placeholder: "比如：身体不适、睡过头、临时有事……", incomplete: "请填写未到课原因" },
};

// 沿用后端参与情况枚举；参考弹窗的插画只负责展示，不改变保存内容。

function reviewSteps(participation, untimed = false, reason = "unspecified") {
  if (untimed) {
    if (reason === "not_attended") return ["untimed_reason", "absence"];
    if (reason !== "forgotten") return ["untimed_reason"];
    return ["untimed_reason", ...reviewSteps(participation === "not_attended" ? "unspecified" : participation)];
  }
  if (["followed", "partial"].includes(participation)) return ["participation", "focus", "learning"];
  if (participation === "not_learning") return ["participation", "focus", "activity"];
  if (participation === "low_content") return ["participation", "activity"];
  if (participation === "not_attended") return ["participation", "absence"];
  return ["participation"];
}

function initialData() {
  return { art, owner: null, loggedIn: false, loading: false, loadingLabel: "", busy: false, error: "", draftError: "", draftReadFailed: false,
    record: null, answers: ["", "", ""], filled: 0, cloud: null,
    dirty: false, conflict: false, pending: false, readonly: false, statusLabel: "本机草稿",
    evaluation: null, reviewResult: null, evaluationError: "", evaluationLoading: false, evaluationPending: false, revision: false,
    canRevise: false, courseNotEnded: false, correctionDeadline: "", clarificationAnswer: "", settlement: null,
    detailsExpanded: false, answersExpanded: false,
    modern: false, participation: "unspecified", clarificationParticipation: "unspecified",
    untimed: false, untimedReason: "unspecified", untimedReasonOptions: UNTIMED_REASONS,
    untimedReasonLabel: "未选择", cloudUntimedReasonLabel: "未选择", wizardParticipationOptions: PARTICIPATION_OPTIONS,
    focusOptions: focus.options, focusChoice: "unspecified", focusLabel: "", cloudFocusLabel: "", clarificationFocus: "unspecified",
    focusStars: focus.stars, focusRating: 0, clarificationFocusRating: 0, showFocus: false, showDescription: false,
    wizard: false, wizardPage: "participation", wizardAnswered: false, wizardHasNext: false, wizardDisabledReason: "",
    wizardPageNumbers: [], wizardPageNumber: 1, wizardTextTitle: "", wizardTextPlaceholder: "",
    historyScroll: 0, submitDisabledReason: "", revisionDisabledReason: "", clarificationDisabledReason: "",
    participationOptions: PARTICIPATION_OPTIONS,
    questions: LEGACY_QUESTIONS.map(item => Object.assign({}, item)) };
}


module.exports = function createReviewController() { return {
  data: initialData(),
  onLoad() {
    this._loadId = 0; this._selectionId = 0; this._evaluationId = 0; this._pollDelay = 4000;
    this.unsubscribe = session.subscribe(() => this.syncIdentity());
    this.syncIdentity();
  },
  onShow() {
    this._visible = true;
    const snapshot = this.syncIdentity();
    if (!snapshot.owner || !this.data.sessionId) return;
    const selectedId = this.data.sessionId;
    if (!this.data.record || selectedId !== this.data.record.id) {
      this.rememberDraft(); this.stopEvaluation(); this._loadId += 1; this._selectionId += 1;
      this.setData({ busy: false, error: "", detailsExpanded: false, answersExpanded: false });
      this.render(null);
      try {
        const value = reviews.state(selectedId);
        if (value.cloud) this.render(value.cloud.session);
      } catch (_) { this.render({ id: selectedId }); }
    }
    return this.reload();
  },
  onHide() { this.rememberDraft(); this._visible = false; this.stopEvaluation(); },
  onUnload() { this.onHide(); this._disposed = true; this._loadId += 1; this._selectionId += 1; if (this.unsubscribe) this.unsubscribe(); },
  syncIdentity() {
    const current = timer.identity();
    if (!this._snapshot || current.owner !== this._snapshot.owner || current.generation !== this._snapshot.generation) {
      this._loadId += 1; this._selectionId += 1; this._preferred = null;
      this._failedSelectionId = null;
      this.stopEvaluation();
      this.setData(initialData());
    }
    this._snapshot = current;
    this.setData({ owner: current.owner, loggedIn: Boolean(current.owner) });
    return current;
  },
  active(snapshot) { return !this._disposed && Boolean(snapshot) && timer.isCurrent(snapshot); },
  render(record) {
    if (!this.active(this._snapshot)) return;
    if (!record) { this.stopEvaluation(); this.setData({ record: null, answers: ["", "", ""], filled: 0, cloud: null, evaluation: null, reviewResult: null, settlement: null, wizard: false, wizardPageNumbers: [] }); return; }
    if (!this.data.record || this.data.record.id !== record.id) this.setData({ wizardPage: record.source === "untimed" ? "untimed_reason" : "participation", evaluation: null, reviewResult: null, evaluationError: "", evaluationLoading: false, settlement: null, canRevise: false, clarificationAnswer: "", detailsExpanded: false, answersExpanded: false });
    this.setData({ record: displayRecord(record) });
    try {
      let value = reviews.state(record.id);
      const submitted = value.cloud && value.cloud.status === "pending_evaluation";
      const envelope = value.evaluation;
      // Another device may already have revised the answers. Do not display the
      // previous version's score beside the newly loaded input while polling.
      const evaluation = envelope && envelope.evaluation && value.cloud
        && (envelope.evaluation.result_review_version || envelope.evaluation.review_version) === value.cloud.version ? envelope.evaluation : null;
      const canReanswer = Boolean(evaluation && evaluation.status === "invalid_answer" && envelope.can_revise);
      if (submitted && value.revision && !canReanswer && Array.isArray(value.cloud.answers)) {
        // Older clients may have left an unsubmitted revision locally. Preserve
        // that draft in storage while showing the submitted record as read-only.
        value = { ...value, answers: value.cloud.answers, questionVersion: value.cloud.question_version,
          reviewContext: value.cloud.review_context || {}, revision: false, dirty: false };
      }
      if (evaluation && evaluation.status === "invalid_answer" && envelope.can_revise && !value.revision
        && !value.pending && !value.evaluationPending && !value.conflict && !this.data.busy) {
        value = reviews.beginRevision(record.id);
        this.setData({ wizardPage: evaluation.invalid_step || "learning" });
      }
      const settlement = evaluation && evaluation.status === "succeeded" && evaluation.settlement;
      const evaluationLabel = evaluation && evaluation.status === "succeeded" && evaluation.outcome ? "已处理"
        : evaluation && evaluation.status === "succeeded" && !settlement ? "等待结算" : evaluation && EVALUATION_LABELS[evaluation.status];
      const participationLabel = (choice) => choice === "uncertain" ? "暂时说不清" : (this.data.participationOptions.find((item) => item.value === choice) || { label: "未单独选择，按文字记录" }).label;
      const focusChoice = value.questionVersion === reviews.BRANCH_VERSION ? (value.reviewContext || {}).focus || "unspecified" : focus.choice(value.answers[2]);
      const participation = (value.reviewContext || {}).participation || "unspecified";
      const focusRating = focus.rating(focusChoice);
      this.setData({ answers: value.answers, filled: value.answers.filter((answer) => answer.trim()).length,
        focusRating, clarificationFocusRating: focus.rating(focus.choice(value.clarificationAnswer)),
        showFocus: focus.appliesTo(participation),
        showDescription: participation !== "unspecified" && (!focus.appliesTo(participation) || focusRating > 0),
        focusChoice, focusLabel: focus.label(focusChoice), cloudFocusLabel: focus.label(value.cloud && value.cloud.review_context && value.cloud.review_context.focus),
        clarificationFocus: focus.choice(value.clarificationAnswer),
        cloud: value.cloud ? Object.fromEntries([
          "status", "version", "question_version", "answers", "voided", "note", "note_version",
          "can_submit", "evaluation_loaded", "correction_deadline",
        ].filter(key => value.cloud[key] !== undefined).map(key => [key, value.cloud[key]])) : null, dirty: value.dirty, conflict: value.conflict, pending: Boolean(value.pending),
        courseNotEnded: Boolean(value.cloud && Date.parse(value.cloud.session.ends_at) > Date.parse(value.cloud.session.server_now)),
        readonly: Boolean((submitted && !value.revision) || value.pending || value.evaluationPending || this.data.busy || value.cloud && value.cloud.voided), draftError: value.storageError, draftReadFailed: false,
        evaluation: evaluation ? Object.assign(Object.fromEntries([
          "id", "status", "prompt_version", "error", "question", "flow", "invalid_step", "outcome",
          "missing_information", "clarification",
        ].filter(key => evaluation[key] !== undefined).map(key => [key, evaluation[key]])), {
          error: displayError(evaluation.error),
          statusLabel: evaluationLabel || "读取评估状态",
          dialogue: (evaluation.dialogue || []).map((turn) => ({ ...turn, participationLabel: turn.participation && turn.participation !== "unspecified" ? participationLabel(turn.participation) : "" })),
        }) : null,
        reviewResult: submitted && !value.revision && !value.pending && !value.evaluationPending && !value.conflict
          && (!value.cloud.voided || evaluation && evaluation.outcome) ? reviewSignal.resultView(evaluation) : null,
        evaluationPending: Boolean(value.evaluationPending), revision: value.revision,
        canRevise: canReanswer || Boolean(evaluation && evaluation.status === "needs_input" && envelope.can_revise),
        correctionDeadline: classIndex.dateTime((envelope && envelope.correction_deadline) || (value.cloud && value.cloud.correction_deadline), record.timezone),
        clarificationAnswer: value.clarificationAnswer, clarificationParticipation: value.clarificationParticipation,
        modern: value.questionVersion === reviews.BRANCH_VERSION, participation,
        untimed: record.source === "untimed", untimedReason: (value.reviewContext || {}).untimed_reason || "unspecified",
        untimedReasonLabel: untimedReasonLabel((value.reviewContext || {}).untimed_reason),
        cloudUntimedReasonLabel: untimedReasonLabel(value.cloud && value.cloud.review_context && value.cloud.review_context.untimed_reason),
        participationLabel: participationLabel((value.reviewContext || {}).participation), cloudParticipationLabel: participationLabel(value.cloud && value.cloud.review_context && value.cloud.review_context.participation),
        settlement: settlement ? classIndex.eventView(settlement) : null,
        statusLabel: value.cloud && value.cloud.voided ? (evaluation && evaluation.outcome ? "已记录 · 已处理" : "已更正参与情况 · 原答案保留") : value.pending || value.evaluationPending ? "操作待确认" : value.conflict ? "答案有冲突" : value.revision ? "正在修订 · 未提交" : submitted ? `已提交 · ${evaluationLabel || "状态待更新"}`
          : value.dirty ? "草稿已保存在本机，尚未提交" : value.cloud && value.cloud.version > 0 ? "草稿已保存云端" : "待填写记录" });
      this.syncControls();
    } catch (error) {
      this.setData({ answers: ["", "", ""], filled: 0, cloud: null, pending: false, conflict: false, readonly: true,
        reviewResult: null, draftError: error.message, draftReadFailed: true });
    }
  },
  async reload() {
    const snapshot = this.syncIdentity();
    if (!snapshot.owner || this.data.busy || !this.data.sessionId) return;
    const loadId = ++this._loadId, selection = this._selectionId, target = this.data.sessionId;
    const current = () => this.active(snapshot) && loadId === this._loadId
      && selection === this._selectionId && this.data.sessionId === target;
    this.setData({ loading: true, error: "" });
    try {
      const value = await reviews.load(target);
      if (!current()) return;
      this.render(value.cloud.session); this.resumeEvaluation();
    } catch (error) { if (current()) this.setData({ error: error.message || "记录暂时无法刷新，请重试。" }); }
    finally { if (current()) this.setData({ loading: false }); }
  },
  answer(event) {
    const started = metrics.now();
    if (!this.active(this._snapshot) || !this.data.record || this.data.readonly || this.data.draftReadFailed) return;
    const { index: rawIndex, recordId, owner } = event.currentTarget.dataset;
    if (recordId !== this.data.record.id || owner !== this._snapshot.owner) return;
    const index = Number(rawIndex);
    if (!Number.isInteger(index) || index < 0 || index > 2 || typeof event.detail.value !== "string") return;
    const answers = this.data.answers.slice(); answers[index] = event.detail.value.slice(0, this.data.modern ? 8000 : 2000);
    const value = reviews.edit(recordId, answers);
    this.setData({ answers: value.answers, filled: value.answers.filter((a) => a.trim()).length,
      dirty: value.dirty, draftError: value.storageError, statusLabel: value.storageError ? "本机保存失败，请重试" : "草稿已保存在本机，尚未提交" }, () => metrics.record("input_render", started));
    this.syncControls();
  },
  answerWizardText(event) {
    if (!this.validWizardEvent(event) || !TEXT_STEPS[this.data.wizardPage]
      || event.currentTarget.dataset.step !== this.data.wizardPage
      || event.currentTarget.dataset.participation !== this.data.participation) return;
    this.answer(event);
  },
  rememberDraft() {
    if (!this.active(this._snapshot) || !this.data.record || this.data.draftReadFailed) return false;
    try {
      // Inputs are persisted synchronously. Only retry a failed write: a later
      // storage quota failure must not trap a draft that is already durable.
      if (reviews.state(this.data.record.id).storageError) reviews.retryLocal(this.data.record.id);
      this.render(this.data.record); return !this.data.draftError;
    } catch (error) {
      this.setData({ draftError: error.message, draftReadFailed: true, readonly: true }); return false;
    }
  },
  retryDraft() { if (this.data.draftReadFailed) return this.reload(); return this.rememberDraft(); },
  requestClose() {
    if (this.data.busy) return false;
    if (this.data.record && !this.rememberDraft()) return false;
    this.onHide();
    this.triggerEvent("close"); return true;
  },
  toggleDetails() { this.setData({ detailsExpanded: !this.data.detailsExpanded }); },
  toggleAnswers() { this.setData({ answersExpanded: !this.data.answersExpanded }); },
  async save(operation) {
    if (!this.active(this._snapshot) || !this.data.record || this.data.busy || this.data.loadingLabel || this.data.draftReadFailed) return;
    const snapshot = this._snapshot, record = this.data.record;
    this._loadId += 1; this._selectionId += 1; this.stopEvaluation();
    const selection = this._selectionId;
    const current = () => this.active(snapshot) && selection === this._selectionId;
    this.setData({ busy: true, loading: false, error: "", readonly: true });
    this.syncControls();
    try {
      await reviews.write(record.id, operation);
      if (current()) wx.showToast({ title: reviews.state(record.id).cloud.status === "pending_evaluation" ? "本次记录已保存" : "草稿已保存云端", icon: "none" });
    } catch (error) { if (current()) this.setData({ error: error.message }); }
    finally { if (current()) { this.setData({ busy: false }); this.render(record); this.refreshEvaluation(); } }
  },
  keepDraft() { return this.save("save"); },
  retryPending() { return this.save("retry"); },
  submitReview() {
    if (!this.active(this._snapshot) || !this.data.record || this.data.submitDisabledReason) return;
    const snapshot = this._snapshot, id = this.data.record.id;
    if (this.data.cloud && this.data.cloud.can_submit === false) return;
    if (this.data.modern) { reviews.editContext(id, { finish_without_score: false }); return this.save("submit"); }
    wx.showModal({ title: "提交本次记录？", content: "提交后先整理课堂总结，再由多空 Agent 综合判断方向与强度，按固定规则结算上课指数；提交后可回看记录与结算结果。",
      confirmText: "确认提交", success: (result) => {
        if (result.confirm && this.active(snapshot) && this.data.record && this.data.record.id === id) this.save("submit");
      } });
  },
  keepLocal() {
    if (!this.active(this._snapshot) || !this.data.record || this.data.busy) return;
    try { reviews.resolve(this.data.record.id, "local"); this.render(this.data.record); return this.data.revision ? this.saveEvaluation("revise") : this.keepDraft(); }
    catch (error) { this.setData({ error: error.message }); }
  },
  useCloud() {
    if (!this.active(this._snapshot) || !this.data.record || this.data.busy) return;
    const snapshot = this._snapshot, id = this.data.record.id;
    wx.showModal({ title: "使用云端答案？", content: "此操作将用下方展示的云端答案替换本机未同步的修改。", success: (result) => {
      if (!result.confirm || !this.active(snapshot) || !this.data.record || this.data.record.id !== id) return;
      try { reviews.resolve(id, "cloud"); this.setData({ error: "" }); this.render(this.data.record); }
      catch (error) { this.setData({ error: error.message }); }
    } });
  },
  goLogin() { wx.switchTab({ url: "/pages/settings/settings" }); },
  stopEvaluation() {
    this._evaluationId = (this._evaluationId || 0) + 1;
    if (this._evaluationTimer) clearTimeout(this._evaluationTimer);
    this._evaluationTimer = null;
  },
  resumeEvaluation() {
    if (this.data.cloud && this.data.cloud.evaluation_loaded) {
      this.stopEvaluation();
      if (this._visible && this.data.evaluation && ["queued", "running"].includes(this.data.evaluation.status)) {
        this._evaluationTimer = setTimeout(() => this.refreshEvaluation(), this._pollDelay);
        if (this._evaluationTimer.unref) this._evaluationTimer.unref();
      }
      return;
    }
    return this.refreshEvaluation();
  },
  async refreshEvaluation() {
    this.stopEvaluation();
    if (!this._visible || !this.active(this._snapshot) || !this.data.record || !this.data.cloud
      || this.data.cloud.voided && !(this.data.evaluation && this.data.evaluation.outcome)
      || this.data.cloud.status !== "pending_evaluation" || this.data.busy) return;
    const snapshot = this._snapshot, record = this.data.record, id = this._evaluationId;
    const current = () => this._visible && this.active(snapshot) && id === this._evaluationId && this.data.record && this.data.record.id === record.id;
    this.setData({ evaluationLoading: true, evaluationError: "" });
    try {
      await reviews.loadEvaluation(record.id);
      if (!current()) return;
      this.render(record);
      const evaluation = this.data.evaluation;
      if (evaluation && ["queued", "running"].includes(evaluation.status)) {
        this._evaluationTimer = setTimeout(() => { if (current()) this.refreshEvaluation(); }, this._pollDelay);
        this._pollDelay = Math.min(20000, this._pollDelay * 1.5);
        if (this._evaluationTimer && this._evaluationTimer.unref) this._evaluationTimer.unref();
      } else this._pollDelay = 4000;
    } catch (error) { if (current()) this.setData({ evaluationError: error.message }); }
    finally { if (current()) this.setData({ evaluationLoading: false }); }
  },
  clarificationInput(event) {
    if (!this.active(this._snapshot) || !this.data.record || this.data.busy || this.data.evaluationPending) return;
    const { recordId, owner } = event.currentTarget.dataset;
    if (recordId !== this.data.record.id || owner !== this._snapshot.owner) return;
    const value = reviews.editClarification(recordId, event.detail.value);
    this.setData({ clarificationAnswer: value.clarificationAnswer, draftError: value.storageError }); this.syncControls();
  },
  async saveEvaluation(operation) {
    if (!this.active(this._snapshot) || !this.data.record || this.data.busy || this.data.loadingLabel || this.data.draftReadFailed) return;
    const snapshot = this._snapshot, record = this.data.record;
    this._loadId += 1; this._selectionId += 1; this.stopEvaluation();
    const selection = this._selectionId;
    const current = () => this.active(snapshot) && selection === this._selectionId;
    this.setData({ busy: true, readonly: true, evaluationError: "" });
    this.syncControls();
    try { await reviews.writeEvaluation(record.id, operation); }
    catch (error) { if (current()) this.setData({ evaluationError: error.message }); }
    finally { if (current()) { this.setData({ busy: false }); this.render(record); if (!this.data.evaluationPending && !this.data.evaluationError) this.refreshEvaluation(); } }
  },
  retryEvaluation() { return this.saveEvaluation("retry"); },
  retryEvaluationPending() { return this.saveEvaluation("resume"); },
  submitClarification() { return this.saveEvaluation("clarify"); },
  submitRevision() {
    if (!this.data.revision || this.data.revisionDisabledReason || !this.data.evaluation || this.data.evaluation.status !== "invalid_answer") return;
    return this.saveEvaluation("revise");
  },
  explainEvaluation() {
    const maximum = scoring.maxima(this.data.evaluation);
    if (!this.data.evaluation || this.data.evaluation.prompt_version === "class-summary-v7") {
      wx.showModal({ title: "记录与结算说明", content: "先结合课程名称和填写内容整理课堂活动、收获、理解与专注，再由多空 Agent 综合进展与不足，给出一次利多、利空或中性判断及0–100强度。利多记正、利空记负、中性记零，课程涨跌率＝3%×有符号强度÷100，再执行当日限幅。没有质量分或计时40%加权；忘记计时仍保留零时长。未到课继续按理由自动免罚或按缺席规则处理。无关回答退回重填，服务失败可重试，无效回答可在课后7天内重新提交。", showCancel: false });
      return;
    }
    if (this.data.evaluation.prompt_version === "class-summary-v6") {
      wx.showModal({ title: "记录与结算说明", content: "先结合课程名称和填写内容整理课堂活动、收获、理解与专注，再由两位经理独立判断利多、利空或中性及0–100强度。利多记正、利空记负、中性记零，课程涨跌率＝3%×两位有符号强度之和÷200，再执行当日限幅。没有质量分或计时40%加权；忘记计时仍保留零时长。未到课继续按理由自动免罚或按缺席规则处理。无关回答退回重填，服务失败可重试，无效回答可在课后7天内重新提交。", showCancel: false });
      return;
    }
    if (this.data.evaluation.prompt_version === "class-quality-v5") {
      wx.showModal({ title: "记录与评分说明", content: "根据本次选项、星级和文字直接判断，相关的简短回答也会评分。回忆30、理解与反思40、专注30分，不按字数加分，不检测手机行为。没听课和水课按实际活动评估，自学、作业可以计入；明确没有学习时质量为0。未到课根据理由自动处理：合理或含糊理由、停课免罚，明确无故缺席沿用缺席规则。忘记计时仍按零时长计算。完全无关的回答需要重填；服务故障保留回答并重试。无效回答可在课后7天内重新提交。", showCancel: false });
      return;
    }
    wx.showModal({ title: "记录与评分说明", content: `输入自动保存在本机，也可保存到云端。本次质量分由回忆 ${maximum.recall}、理解与反思 ${maximum.understanding}、专注 ${maximum.focus} 分组成，不按字数加分，不检测手机行为。新提交采用 30／40／30，历史评分保留原规则。新版最多补问两次，暂时说不清最多一次；旧三问最多一次。可随时保存结束，资料不足不等于零分。明确到课但未参与学习时，本次质量按 0 分记录，不代表原有知识或个人能力。计时占 40%、质量占 60%。课后 7 天内可提交记录，提交后可回看结果。`, showCancel: false });
  },
  syncControls() {
    const d = this.data;
    const wizard = Boolean(d.modern && d.record && !d.conflict
      && !(d.cloud && d.cloud.voided) && (!d.cloud || d.cloud.status === "draft" || d.revision));
    const steps = reviewSteps(d.participation, d.untimed, d.untimedReason);
    const wizardPage = steps.includes(d.wizardPage) ? d.wizardPage : steps[0];
    const wizardParticipationOptions = d.untimed ? PARTICIPATION_OPTIONS.filter((item) => item.value !== "not_attended") : PARTICIPATION_OPTIONS;
    const pageIndex = steps.indexOf(wizardPage);
    const textStep = TEXT_STEPS[wizardPage];
    const textTitle = wizardPage === "activity" && d.participation === "not_learning"
      ? "没听课的话，这节课在做什么呢？" : textStep ? textStep.title : "";
    const requiredText = TEXT_STEPS[steps[steps.length - 1]];
    const textAnswered = Boolean((d.answers[0] || "").trim());
    const reasonAnswered = UNTIMED_REASONS.some((item) => item.value === d.untimedReason);
    const wizardAnswered = textStep ? textAnswered : wizardPage === "untimed_reason" ? reasonAnswered
      : wizardPage === "focus" ? d.focusRating > 0 : wizardParticipationOptions.some((item) => item.value === d.participation);
    const wizardHasNext = pageIndex < steps.length - 1;
    const common = d.busy ? "正在保存，请稍候" : d.loadingLabel ? "正在切换记录，请稍候" : d.pending || d.evaluationPending ? "请先确认上次操作结果" : d.draftReadFailed ? "请先恢复草稿读取" : d.conflict ? "请先核对本机和云端内容" : !d.cloud ? "正在读取云端状态" : d.cloud.voided ? "参与情况已更正，原答案仅供回看" : "";
    const incomplete = !d.modern && d.filled !== 3 ? "旧版还有未填写项，也可切换为一段描述" : "";
    const stepIncomplete = d.modern ? d.untimed && !reasonAnswered ? "请选择未计时原因"
      : d.participation === "unspecified" || d.untimed && d.untimedReason === "forgotten" && d.participation === "not_attended" ? "请先选择这节课的情况" : d.showFocus && !d.focusRating ? "请给自己的专注情况打星，也可以先保存结束"
      : requiredText && !textAnswered ? requiredText.incomplete : "" : "";
    const ended = d.cloud && d.cloud.can_submit === false ? d.courseNotEnded ? "课程尚未结束，可先保存草稿" : "已超过提交期限，仍可保存草稿" : "";
    this.setData({ wizard, wizardPage, wizardAnswered, wizardHasNext, wizardParticipationOptions,
      wizardPageNumbers: wizard ? steps.map((_, index) => index + 1) : [], wizardPageNumber: pageIndex + 1,
      wizardTextTitle: textTitle, wizardTextPlaceholder: textStep ? textStep.placeholder : "",
      submitDisabledReason: common || ended || incomplete || stepIncomplete,
      revisionDisabledReason: common || (!d.canRevise ? "已超过重新提交期限，仍可回看记录" : "") || (!d.dirty ? "内容与已提交版本相同，可直接返回" : "") || incomplete || stepIncomplete,
      clarificationDisabledReason: common || (!d.canRevise ? "已超过补充期限，可以直接结束" : "")
        || (d.evaluation && d.evaluation.flow && d.evaluation.flow.key === "focus"
          ? d.clarificationFocus === "unspecified" ? "请选择本次专注情况" : ""
          : !d.clarificationAnswer.trim() && d.clarificationParticipation === "unspecified" ? "请填写一句说明或选择实际情况" : "") });
    this.setData({ wizardDisabledReason: common || d.draftError
      || (!wizardAnswered ? textStep ? textStep.incomplete : wizardPage === "untimed_reason" ? "请选择未计时原因" : wizardPage === "focus" ? "请给自己的专注情况打星" : "请先选择这节课的情况" : "")
      || (wizardHasNext ? "" : d.revision ? d.revisionDisabledReason : d.submitDisabledReason) });
  },
  validWizardEvent(event) {
    const data = event && event.currentTarget && event.currentTarget.dataset || {};
    return this.active(this._snapshot) && this.data.wizard && this.data.record
      && data.owner === this.data.owner && data.recordId === this.data.record.id;
  },
  nextReviewPage(event) {
    if (!this.validWizardEvent(event) || !this.data.wizardHasNext || this.data.wizardDisabledReason || !this.rememberDraft()) return;
    const steps = reviewSteps(this.data.participation, this.data.untimed, this.data.untimedReason);
    this.setData({ wizardPage: steps[steps.indexOf(this.data.wizardPage) + 1] }); this.syncControls();
  },
  previousReviewPage(event) {
    if (!this.validWizardEvent(event) || this.data.wizardPageNumber <= 1 || this.data.readonly || this.data.loadingLabel || !this.rememberDraft()) return;
    const steps = reviewSteps(this.data.participation, this.data.untimed, this.data.untimedReason);
    this.setData({ wizardPage: steps[steps.indexOf(this.data.wizardPage) - 1] }); this.syncControls();
  },
  submitWizard(event) {
    if (!this.validWizardEvent(event) || this.data.wizardHasNext || this.data.wizardDisabledReason) return;
    return this.data.revision ? this.submitRevision() : this.submitReview();
  },
  chooseUntimedReason(event) {
    if (!this.validWizardEvent(event) || !this.data.untimed || this.data.wizardPage !== "untimed_reason"
      || this.data.readonly || this.data.draftReadFailed) return;
    const reason = event.detail && typeof event.detail.value === "string" ? event.detail.value : event.currentTarget.dataset.value;
    if (!UNTIMED_REASONS.some((item) => item.value === reason)) return;
    const context = { untimed_reason: reason, finish_without_score: false };
    if (reason === "not_attended") Object.assign(context, { participation: "not_attended", focus: "unspecified" });
    else if (this.data.participation === "not_attended") Object.assign(context, { participation: "unspecified", focus: "unspecified" });
    reviews.editContext(this.data.record.id, context); this.render(this.data.record);
  },
  chooseParticipation(event) {
    const { owner, recordId } = event.currentTarget.dataset;
    if (!this.active(this._snapshot) || !this.data.record || this.data.readonly || this.data.draftReadFailed
      || owner !== this.data.owner || recordId && recordId !== this.data.record.id
      || this.data.wizard && this.data.wizardPage !== "participation") return;
    const choice = event.detail && typeof event.detail.value === "string" ? event.detail.value : event.currentTarget.dataset.value;
    if (this.data.untimed && (this.data.untimedReason !== "forgotten" || choice === "not_attended")) return;
    if (!["unspecified", ...this.data.participationOptions.map((o) => o.value)].includes(choice)) return;
    const context = { participation: choice, finish_without_score: false };
    if (!focus.appliesTo(choice)) context.focus = "unspecified";
    reviews.editContext(this.data.record.id, context); this.render(this.data.record);
  },
  chooseFocus(event) {
    const { value, owner, recordId } = event.currentTarget.dataset;
    if (!this.active(this._snapshot) || !this.data.record || this.data.readonly || this.data.draftReadFailed
      || owner !== this.data.owner || recordId !== this.data.record.id || !focus.label(value)
      || this.data.modern && !this.data.showFocus || this.data.wizard && this.data.wizardPage !== "focus") return;
    if (this.data.modern) reviews.editContext(recordId, { focus: value, finish_without_score: false });
    else { const answers = this.data.answers.slice(); answers[2] = focus.label(value); reviews.edit(recordId, answers); }
    this.render(this.data.record);
  },
  chooseFocusClarification(event) {
    const { value, owner, recordId } = event.currentTarget.dataset;
    if (!this.active(this._snapshot) || !this.data.record || this.data.busy || this.data.evaluationPending
      || !this.data.canRevise || owner !== this.data.owner || recordId !== this.data.record.id || !focus.label(value)
      || !this.data.evaluation || this.data.evaluation.status !== "needs_input" || !this.data.evaluation.flow || this.data.evaluation.flow.key !== "focus") return;
    reviews.editClarification(recordId, focus.label(value)); this.render(this.data.record);
  },
  upgradeReview() { if (!this.data.record || this.data.readonly) return; reviews.upgrade(this.data.record.id); this.render(this.data.record); },
  chooseClarification(event) {
    if (!this.data.record || this.data.busy || this.data.evaluationPending || event.currentTarget.dataset.owner !== this.data.owner) return;
    const choice = event.currentTarget.dataset.value;
    if (!this.data.participationOptions.some((o) => o.value === choice)) return;
    reviews.editChoice(this.data.record.id, choice); this.render(this.data.record);
  },
  async finishRecord() {
    if (!this.data.record || this.data.busy || this.data.pending || this.data.evaluationPending || this.data.conflict) return;
    if (this.data.cloud && this.data.cloud.status === "pending_evaluation") {
      if (this.data.evaluation && this.data.evaluation.status === "needs_input") await this.saveEvaluation("finish");
    } else {
      if (!this.data.modern) reviews.upgrade(this.data.record.id);
      reviews.editContext(this.data.record.id, { finish_without_score: false });
      await this.save("save");
    }
    if (!this.data.error && !this.data.evaluationError && !this.data.pending && !this.data.evaluationPending) this.requestClose();
  },
}; };
