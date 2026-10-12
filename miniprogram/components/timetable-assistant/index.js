const agent = require("../../services/timetable-agent");
const session = require("../../services/session");
const { displayText } = require("../../services/timetable-preview");
const { presentResult } = require("../../services/timetable-agent-result");
const timetableChange = require("../../services/timetable-change");
const RUNNING = ["queued", "running", "confirm_queued", "confirming"];
const STATUS = { queued: "正在等待识别与整理…", running: "正在读取全部图片、整理并保存课程…", confirm_queued: "正在完成旧版已确认任务…", confirming: "正在完成旧版已确认任务…", needs_input: "旧版任务已停止，请发送新的课表说明", awaiting_confirmation: "旧版任务待更新，请发送新的课表说明", applied: "已保存到个人课表", cancelled: "本次处理已取消", failed: "这次处理未完成，可以重试", stale: "课表已有变化，未应用的修改已保留", completed: "处理已完成", superseded: "旧版任务已替代，原始输入已保留" };
function initialData() {
  return { loggedIn: false, loading: false, busy: false, uploading: false, error: "", threads: [], threadIndex: 0, thread: null, messages: [], text: "", images: [], processing: false, statusText: "", reply: "", replyText: "", replyExpanded: false, replyExpandable: false, runError: "", unappliedChanges: [], unappliedCount: 0, unappliedExpanded: false, diagnosticRows: [], diagnosticCount: 0, diagnosticsExpanded: false, periodTimeSetupRequired: false, periodTimeSetupTables: [], periodInputFocused: false, scrollTarget: "", canRetry: false, pending: false, needsReload: false, keyboardHeight: 0 };
}
Component({
  properties: { semesterId: { type: String, value: "" }, threadId: { type: String, value: "" } },
  data: initialData(),
  lifetimes: {
    attached() {
      // 每次打开创建独立实例，学期范围在这一轮对话中保持不变。
      this._semesterId = this.properties.semesterId || "";
      this._scope = this._semesterId || "all";
      this._snapshot = agent.identity(); this._epoch = 0;
      this._visible = false; this._disposed = false; this._closing = false;
      this._pageHidden = false; this._choosingImages = false;
      this._pending = null; this._appliedRuns = {};
      this.unsubscribe = session.subscribe(() => { if (!this._disposed) this.syncIdentity(); });
      this.restore();
      if (this.properties.threadId && !this._pending && !this.data.text && !this.data.images.length) this.setData({ thread: { id: this.properties.threadId } });
      return this.resume();
    },
    detached() {
      this.pause(); this._disposed = true;
      if (this.unsubscribe) this.unsubscribe();
      this.unsubscribe = null;
    },
  },
  pageLifetimes: {
    show() { return this.resume(); },
    hide() {
      this._pageHidden = true;
      // 相册/相机可能触发页面隐藏；保留本轮 epoch，让选图回调继续上传。
      if (this._choosingImages) { this.stopPolling(); this.setData({ keyboardHeight: 0 }); return; }
      this.pause();
    },
  },
  methods: {
    resume() {
      if (this._disposed || this._closing) return;
      this._pageHidden = false;
      if (this._visible) { this.syncIdentity(); this.schedulePoll(); return; }
      this._visible = true;
      this.syncIdentity();
      return this.reload();
    },
    pause() {
      if (this._disposed) return;
      this.remember(); this._visible = false; this._epoch += 1;
      this.stopPolling(); this.setData({ keyboardHeight: 0 });
    },
    closeAssistant() {
      if (this._disposed || this._closing) return;
      this.pause(); this._closing = true;
      if (typeof wx.hideKeyboard === "function") wx.hideKeyboard();
      this.triggerEvent("close");
    },
    noop() {},
    onKeyboardHeightChange(event) {
      if (this._disposed || this._closing) return;
      const height = Number(event.detail && event.detail.height) || 0;
      this.setData({ keyboardHeight: Math.max(0, height) });
    },
    active(snapshot, epoch) { return !this._disposed && !this._closing && this._visible && agent.isCurrent(snapshot) && (epoch == null || epoch === this._epoch); },
    restore() {
      this._displayKey = "";
      const draft = agent.recall(this._scope);
      this._pending = draft && draft.pending || null;
      this.setData(Object.assign(initialData(), { loggedIn: Boolean(this._snapshot.owner), text: draft && draft.text || "", images: draft && draft.images || [], thread: draft && draft.thread || null, pending: Boolean(this._pending) }));
    },
    syncIdentity() {
      const next = agent.identity();
      if (!this._snapshot || next.owner !== this._snapshot.owner || next.generation !== this._snapshot.generation) {
        const sameOwner = this._snapshot && next.owner === this._snapshot.owner;
        this.stopPolling(); this._epoch += 1; this._snapshot = next;
        if (sameOwner) this.setData({ busy: false, loading: false, uploading: false });
        else { this._appliedRuns = {}; this.restore(); }
      }
      this._snapshot = next; this.setData({ loggedIn: Boolean(next.owner) }); return next;
    },
    remember() {
      if (!agent.isCurrent(this._snapshot)) return;
      // 图片仅缓存当前进程内的临时路径；对话正文由服务端恢复。
      agent.remember(this._scope, { text: this.data.text, images: this.data.images, thread: this.data.thread && { id: this.data.thread.id, semester_id: this.data.thread.semester_id, title: this.data.thread.title }, pending: this._pending });
    },
    stopPolling() { if (this._poll) clearTimeout(this._poll); this._poll = null; },
    schedulePoll() {
      this.stopPolling();
      if (!this._visible || this._pageHidden || this._disposed || this._closing || !this.data.processing || this._pending) return;
      this._poll = setTimeout(() => { this._poll = null; this.poll(); }, 2000);
    },
    async poll() {
      const snapshot = this.syncIdentity(); const epoch = this._epoch;
      if (!snapshot.owner || !this.data.thread || !this._visible) return;
      const id = this.data.thread.id;
      try {
        const thread = await agent.get(id, this.data.thread.version);
        if (!this.active(snapshot, epoch) || !this.data.thread || this.data.thread.id !== id) return;
        if (!thread.unchanged) this.displayThread(thread);
        this.schedulePoll();
      } catch (error) { if (this.active(snapshot, epoch)) this.setData({ error: `${error.message} 点击“重新读取”恢复处理状态。` }); }
    },
    displayThread(thread) {
      const run = thread.run || null;
      const input = run && (thread.messages || []).find((message) => message.id === `${run.id}:user`);
      const result = presentResult(run && run.result, input && input.text);
      const key = `${thread.id}:${run && run.id}:${run && run.status}:${run && run.reply}:${JSON.stringify(result)}`;
      const sameResult = key === this._displayKey; this._displayKey = key;
      this._unappliedChanges = result.unapplied; this._diagnosticRows = result.diagnostics;
      const unappliedExpanded = Boolean(sameResult && this.data.unappliedExpanded);
      const diagnosticsExpanded = Boolean(sameResult && this.data.diagnosticsExpanded);
      const previousMessages = this.data.thread && this.data.thread.id === thread.id ? this.data.messages : [];
      const messages = (thread.messages || []).map((message) => {
        const text = message.role === "user" ? message.text : displayText(message.text);
        const expanded = previousMessages.some((item) => item.id === message.id && item.expanded);
        const expandable = message.role !== "user" && text.length > 200;
        return Object.assign({}, message, { text, isUser: message.role === "user", expanded, expandable, displayText: expandable && !expanded ? text.slice(0, 200) + "…" : text });
      });
      const reply = displayText(run && run.reply);
      const replyExpanded = sameResult && this.data.replyExpanded;
      const summaryThread = { id: thread.id, semester_id: thread.semester_id, title: thread.title, version: thread.version, updated_at: thread.updated_at, has_more_messages: thread.has_more_messages };
      const threads = thread.id ? [summaryThread].concat(this.data.threads.filter((item) => item.id !== thread.id)) : this.data.threads;
      thread = { ...summaryThread, run: run ? { id: run.id, status: run.status } : null };
      this.setData({ thread, threads, threadIndex: 0, messages, needsReload: false, processing: Boolean(run && RUNNING.includes(run.status)), statusText: run ? STATUS[run.status] || "正在读取处理状态" : "", replyExpanded, replyExpandable: reply.length > 200, replyText: !replyExpanded && reply.length > 200 ? reply.slice(0, 200) + "…" : reply, canRetry: Boolean(run && run.status === "failed"), reply: messages.some((message) => !message.isUser && message.text === reply) ? "" : reply, runError: displayText(run && run.error),
        unappliedChanges: unappliedExpanded ? result.unapplied : result.unapplied.slice(0, 5), unappliedCount: result.unapplied.length, unappliedExpanded,
        diagnosticRows: diagnosticsExpanded ? result.diagnostics : result.diagnostics.slice(0, 5), diagnosticCount: result.diagnostics.length, diagnosticsExpanded,
        periodTimeSetupRequired: result.periodTimeSetup.required, periodTimeSetupTables: result.periodTimeSetup.tables });
      this.remember();
      if (thread.id) this.triggerEvent("threadchange", { threadId: thread.id, semesterId: thread.semester_id || "" });
      if (run && run.status === "applied" && run.id && !this._appliedRuns[run.id]) {
        this._appliedRuns[run.id] = true;
        if (!run.result || run.result.saved !== false) timetableChange.publish(this._snapshot, { runId: run.id, threadId: thread.id });
        this.triggerEvent("applied", { runId: run.id, threadId: thread.id, semesterId: thread.semester_id || this._semesterId || "" });
      }
    },
    toggleMessage(event) {
      const id = event.currentTarget.dataset.id;
      this.setData({ messages: this.data.messages.map((message) => message.id !== id || !message.expandable ? message : { ...message, expanded: !message.expanded, displayText: message.expanded ? message.text.slice(0, 200) + "…" : message.text }) });
    },
    toggleReply() {
      const expanded = !this.data.replyExpanded;
      this.setData({ replyExpanded: expanded, replyText: expanded ? this.data.reply : this.data.reply.slice(0, 200) + "…" });
    },
    toggleUnapplied() {
      const expanded = !this.data.unappliedExpanded;
      this.setData({ unappliedExpanded: expanded, unappliedChanges: expanded ? this._unappliedChanges : this._unappliedChanges.slice(0, 5) });
    },
    toggleDiagnostics() {
      const expanded = !this.data.diagnosticsExpanded;
      this.setData({ diagnosticsExpanded: expanded, diagnosticRows: expanded ? this._diagnosticRows : this._diagnosticRows.slice(0, 5) });
    },
    preparePeriodTimes() {
      if (this.data.busy || this.data.loading || this.data.uploading || this.data.processing || this.data.pending || this.data.needsReload) return;
      // 只聚焦输入框，保留用户草稿，不自动填入或发送任何时间。
      this.setData({ periodInputFocused: false }, () => this.setData({ periodInputFocused: true }));
    },
    onPeriodInputBlur() { this.setData({ periodInputFocused: false }); },
    async reload() {
      const snapshot = this.syncIdentity(); if (!snapshot.owner || !this._visible) return;
      this.stopPolling(); const epoch = ++this._epoch;
      this.setData({ loading: true, busy: false, uploading: false, error: "" });
      try {
        if (this._pending && !await this.perform(this._pending, snapshot, epoch)) return;
        const threads = await agent.list(); if (!this.active(snapshot, epoch)) return;
        const choices = threads.filter((thread) => this._semesterId ? thread.semester_id === this._semesterId : !thread.semester_id);
        let chosen = choices.find((thread) => this.data.thread && thread.id === this.data.thread.id) || choices[0] || null;
        // 初始化线程确认后可能刚关联学期；已有选择仍通过详情接口鉴权恢复。
        if (!chosen && this.data.thread && this.data.thread.id && (!this.data.thread.semester_id || this.data.thread.semester_id === this._semesterId)) chosen = this.data.thread;
        this.setData({ threads: choices, threadIndex: Math.max(0, choices.findIndex((thread) => chosen && thread.id === chosen.id)) });
        if (chosen) {
          const thread = await agent.get(chosen.id); if (!this.active(snapshot, epoch)) return;
          this.displayThread(thread); this.schedulePoll();
        } else this.displayThread({ id: null, semester_id: this._semesterId || null, title: "新对话", messages: [], run: null });
      } catch (error) { if (this.active(snapshot, epoch)) this.setData({ error: error.message || "无法读取对话，请稍后重试。" }); }
      finally { if (this.active(snapshot, epoch)) this.setData({ loading: false }); }
    },
    async perform(action, snapshot = this._snapshot, epoch = this._epoch) {
      this._pending = action; this.setData({ pending: true, busy: true, error: "" }); this.remember(); this.stopPolling();
      try {
        const thread = await agent.execute(action);
        if (!this.active(snapshot, epoch)) return null;
        if (action.kind === "send") this.setData({ text: "", images: [] });
        this._pending = null; this.setData({ pending: false, busy: false });
        this.displayThread(thread); this.remember(); agent.acknowledge(action); this.schedulePoll();
        return thread;
      } catch (error) {
        if (!this.active(snapshot, epoch)) return null;
        // 4xx 是明确拒绝；超时/网络/5xx 的结果未知，保留原请求重放。
        const rejected = error.statusCode >= 400 && error.statusCode < 500;
        if (rejected) { this._pending = null; this.setData({ pending: false }); agent.acknowledge(action); }
        this.setData({ busy: false, needsReload: error.statusCode === 409, error: error.statusCode === 409 ? "对话或课表已更新。输入已保留，请重新读取最新结果后继续。" : `${error.message || "请求未完成。"}${rejected ? "" : " 结果尚未确认，请点击“重新读取”恢复，勿重复提交。"}` });
        this.remember(); return null;
      }
    },
    async ensureThread(snapshot, epoch) {
      if (this.data.thread && this.data.thread.id) return this.data.thread;
      const data = this._semesterId ? { semester_id: this._semesterId } : {};
      return this.perform({ kind: "create", data }, snapshot, epoch);
    },
    inputText(event) { if (this.data.busy || this.data.pending) return; this.setData({ text: event.detail.value }); this.remember(); },
    async send() {
      if (this.data.busy || this.data.loading || this.data.uploading || this.data.processing || this.data.needsReload || this._pending) return;
      const snapshot = this.syncIdentity(); const epoch = this._epoch; if (!snapshot.owner) return;
      const text = this.data.text.trim(); const images = this.data.images;
      if (!text && !images.length) { this.setData({ error: "请描述课程安排，或选择一张课表截图。" }); return; }
      if (images.some((image) => Date.parse(image.expires_at) <= Date.now())) { this.setData({ error: "有图片已过期，请移除并重新选择；文字输入已保留。" }); return; }
      this.setData({ busy: true, error: "" });
      const thread = await this.ensureThread(snapshot, epoch);
      if (!thread || !this.active(snapshot, epoch)) return;
      return this.perform({ kind: "send", threadId: thread.id, data: { text, image_ids: images.map((image) => image.id), version: thread.version } }, snapshot, epoch);
    },
    async chooseImages() {
      if (this.data.busy || this.data.loading || this.data.uploading || this.data.processing || this._pending || this.data.images.length >= 3) return;
      const snapshot = this.syncIdentity(); const epoch = this._epoch; if (!snapshot.owner) return;
      this.setData({ uploading: true, error: "" });
      this._choosingImages = true;
      try {
        if (!agent.imageNotice()) {
          const choice = await new Promise((resolve) => wx.showModal({ title: "识别课表截图", content: "图片和对话说明将通过服务器发送给 DeepSeek，用于整理你的课表。识别结果将直接保存，你可以随时修改。处理完成后删除图片，未完成的图片保留最多 7 天。", confirmText: "选择图片", success: resolve, fail: () => resolve({ confirm: false }) }));
          if (!choice.confirm || !this.active(snapshot, epoch)) return;
          agent.acceptImageNotice();
        }
        const result = await new Promise((resolve, reject) => {
          const fail = (error) => /cancel/i.test(error.errMsg || "") ? resolve({ tempFiles: [] }) : reject(new Error("无法选择图片，请重试。"));
          if (typeof wx.chooseMedia === "function") wx.chooseMedia({ count: 3 - this.data.images.length, mediaType: ["image"], sourceType: ["album", "camera"], sizeType: ["original"], success: resolve, fail });
          else if (typeof wx.chooseImage === "function") wx.chooseImage({ count: 3 - this.data.images.length, sizeType: ["original"], success: (value) => resolve({ tempFiles: value.tempFilePaths.map((tempFilePath) => ({ tempFilePath })) }), fail });
          else reject(new Error("当前微信无法选择图片，请更新微信后重试。"));
        });
        if (!this.active(snapshot, epoch) || !result.tempFiles.length) return;
        const thread = await this.ensureThread(snapshot, epoch); if (!thread || !this.active(snapshot, epoch)) return;
        // 使用索引循环，避免开发者工具为 for...of 注入缺失的 Babel helper 导致整页加载失败。
        const files = result.tempFiles.slice(0, 3 - this.data.images.length);
        for (let index = 0; index < files.length; index += 1) {
          const file = files[index];
          const saved = await agent.upload(thread.id, file.tempFilePath);
          if (!this.active(snapshot, epoch)) return;
          this.setData({ images: this.data.images.concat(Object.assign({}, saved, { path: file.tempFilePath })) }); this.remember();
        }
      } catch (error) { if (this.active(snapshot, epoch)) this.setData({ error: error.message || "图片上传失败，文字输入已保留。" }); }
      finally {
        this._choosingImages = false;
        if (this.active(snapshot, epoch)) this.setData({ uploading: false, busy: false });
        if (this._pageHidden && !this._disposed && !this._closing) this.pause();
        else this.schedulePoll();
      }
    },
    removeImage(event) { if (this.data.busy || this.data.pending || this.data.uploading) return; this.setData({ images: this.data.images.filter((_, index) => index !== Number(event.currentTarget.dataset.index)) }); this.remember(); },
    previewImage(event) { const index = Number(event.currentTarget.dataset.index); if (this.data.images[index] && typeof wx.previewImage === "function") wx.previewImage({ current: this.data.images[index].path, urls: this.data.images.map((image) => image.path) }); },
    retryRun() {
      if (this.data.busy || this.data.loading || this.data.uploading || this.data.needsReload || this._pending || !this.data.canRetry) return;
      const snapshot = this.syncIdentity(); if (!snapshot.owner) return;
      const thread = this.data.thread;
      return this.perform({ kind: "retry", threadId: thread.id, runId: thread.run.id, data: { version: thread.version } });
    },
    async selectThread(event) {
      if (this.data.busy || this.data.uploading || this._pending) return;
      const thread = this.data.threads[Number(event.detail.value)]; if (!thread || this.data.thread && thread.id === this.data.thread.id) return;
      if (!await this.allowDiscard()) return;
      this.setData({ thread, text: "", images: [] }); this.remember(); return this.reload();
    },
    async allowDiscard() {
      if (!this.data.text && !this.data.images.length) return true;
      const snapshot = this._snapshot; const epoch = this._epoch;
      const choice = await new Promise((resolve) => wx.showModal({ title: "放下这条未发送消息？", content: "切换对话会清空当前未发送的文字和图片。已发送的对话会保留。", confirmText: "切换对话", success: resolve, fail: () => resolve({ confirm: false }) }));
      return choice.confirm && this.active(snapshot, epoch);
    },
    async newThread() {
      if (this.data.busy || this.data.loading || this.data.uploading || this._pending || !await this.allowDiscard()) return;
      const snapshot = this.syncIdentity(); if (!snapshot.owner) return;
      const semesterId = this._semesterId;
      this.stopPolling(); this._epoch += 1; this.setData({ text: "", images: [] });
      return this.perform({ kind: "create", data: semesterId ? { semester_id: semesterId } : {} }, snapshot, this._epoch);
    },
    goLogin() { this.closeAssistant(); wx.switchTab({ url: "/pages/settings/settings" }); },
    goTimetable() { this.closeAssistant(); },
  },
});
