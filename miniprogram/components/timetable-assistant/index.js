const agent = require("../../services/timetable-agent");
const session = require("../../services/session");
const { displayText, summarize } = require("../../services/timetable-preview");
const RUNNING = ["queued", "running", "confirm_queued", "confirming"];
const STATUS = { queued: "正在等待识别与整理…", running: "正在整理课程和日期安排…", confirm_queued: "正在等待应用，尚未写入课表…", confirming: "正在应用已确认的变更…", needs_input: "还需要你补充一些信息", awaiting_confirmation: "请核对下方变更，再确认写入课表", applied: "已应用到个人课表", cancelled: "本次变更已取消，课表保持原状", failed: "这次处理未完成，可以重试或继续说明", stale: "课表已有变化，请继续说明以重新生成预览", completed: "已整理好你的课表信息" };
const LABELS = { name: "名称", title: "名称", start_date: "开始日期", end_date: "结束日期", first_week_monday: "第 1 周周一", weekday: "星期", start_week: "开始周", end_week: "结束周", week_pattern: "周次", start_time: "开始时间", end_time: "结束时间", location: "地点", date: "日期", local_date: "日期", starts_at: "开始", ends_at: "结束", original_start_at: "原上课时间", status: "状态", note: "备注", number: "节次", periods: "节次时间", course_name: "课程", from_date: "原日期", to_date: "调整日期", source_date: "原日期", target_date: "调整日期", duration_minutes: "时长（分钟）", total_weeks: "周数", week_number: "原课程周次" };
const VALUES = { all: "每周", odd: "单周", even: "双周", scheduled: "正常上课", leave: "请假", cancelled: "停课" };
function readable(value) {
  if (value == null || value === "") return "无";
  if (Array.isArray(value)) return value.map(readable).join("；");
  if (typeof value === "object") return Object.keys(value).filter((key) => !["id", "user_id", "semester_id", "course_id", "version", "is_override", "is_deleted", "created_at", "has_courses", "today", "starts_at", "ends_at", "original_start_at", "timezone"].includes(key)).map((key) => `${LABELS[key] || key}：${key === "weekday" ? `周${["", "一", "二", "三", "四", "五", "六", "日"][value[key]] || value[key]}` : readable(value[key])}`).join("\n");
  return displayText(VALUES[value] || String(value));
}
function initialData() {
  return { loggedIn: false, loading: false, busy: false, uploading: false, error: "", threads: [], threadIndex: 0, thread: null, messages: [], text: "", images: [], processing: false, statusText: "", reply: "", replyText: "", replyExpanded: false, replyExpandable: false, runError: "", changes: [], changeCount: 0, previewSummary: "", previewWarning: "", detailsExpanded: false, detailPage: 0, detailPages: 0, scrollTarget: "", canConfirm: false, canRetry: false, pending: false, needsReload: false, keyboardHeight: 0 };
}
Component({
  properties: { semesterId: { type: String, value: "" } },
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
      this._changeRows = []; this._previewKey = "";
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
      const run = thread.run || null; const preview = run && run.preview;
      const rawChanges = preview && Array.isArray(preview.changes) ? preview.changes : [];
      const key = `${thread.id}:${run && run.id}:${run && run.status}:${JSON.stringify(preview)}`;
      const samePreview = key === this._previewKey; this._previewKey = key;
      const changes = rawChanges.map((change, index) => ({ index, title: displayText(change.title || "课表变更"), before: readable(change.before),
        after: change.kind === "delete_lesson" ? "删除这次课程安排" : change.kind === "delete_course" ? "删除每周安排及未记录的课次；已有学习记录保留" : readable(change.after) }));
      this._changeRows = changes;
      const previousMessages = this.data.thread && this.data.thread.id === thread.id ? this.data.messages : [];
      const messages = (thread.messages || []).map((message) => {
        const text = message.role === "user" ? message.text : displayText(message.text);
        const expanded = previousMessages.some((item) => item.id === message.id && item.expanded);
        const expandable = message.role !== "user" && text.length > 200;
        return Object.assign({}, message, { text, isUser: message.role === "user", expanded, expandable, displayText: expandable && !expanded ? text.slice(0, 200) + "…" : text });
      });
      const reply = displayText(run && run.reply);
      const replyExpanded = samePreview && this.data.replyExpanded;
      const detailPage = samePreview ? this.data.detailPage : 0;
      const summary = summarize(rawChanges);
      const summaryThread = { id: thread.id, semester_id: thread.semester_id, title: thread.title, version: thread.version, updated_at: thread.updated_at };
      const threads = thread.id ? [summaryThread].concat(this.data.threads.filter((item) => item.id !== thread.id)) : this.data.threads;
      thread = { ...summaryThread, run: run ? { id: run.id, status: run.status } : null };
      this.setData({ thread, threads, threadIndex: 0, messages, needsReload: false, processing: Boolean(run && RUNNING.includes(run.status)), statusText: run ? STATUS[run.status] || "正在读取处理状态" : "", changes: changes.slice(detailPage * 20, (detailPage + 1) * 20), changeCount: changes.length, previewSummary: summary.summary, previewWarning: summary.warning, detailsExpanded: samePreview && this.data.detailsExpanded, detailPage, detailPages: Math.ceil(changes.length / 20), scrollTarget: samePreview ? this.data.scrollTarget : "", replyExpanded, replyExpandable: reply.length > 200, replyText: !replyExpanded && reply.length > 200 ? reply.slice(0, 200) + "…" : reply, canConfirm: Boolean(run && run.status === "awaiting_confirmation" && preview), canRetry: Boolean(run && run.status === "failed"), reply: messages.some((message) => !message.isUser && message.text === reply) ? "" : reply, runError: displayText(run && run.error) });
      this.remember();
      if (run && run.status === "applied" && run.id && !this._appliedRuns[run.id]) {
        this._appliedRuns[run.id] = true;
        this.triggerEvent("applied", { runId: run.id, threadId: thread.id, semesterId: thread.semester_id || this._semesterId || "" });
      }
    },
    toggleDetails() {
      const expanded = !this.data.detailsExpanded;
      this.setData({ detailsExpanded: expanded, scrollTarget: expanded ? "preview-details" : "" });
    },
    changeDetailPage(event) {
      const page = this.data.detailPage + Number(event.currentTarget.dataset.direction);
      if (!Number.isInteger(page) || page < 0 || page >= this.data.detailPages) return;
      this.setData({ detailPage: page, changes: this._changeRows.slice(page * 20, (page + 1) * 20), scrollTarget: "" });
      this.setData({ scrollTarget: "preview-details" });
    },
    toggleMessage(event) {
      const id = event.currentTarget.dataset.id;
      this.setData({ messages: this.data.messages.map((message) => message.id !== id || !message.expandable ? message : { ...message, expanded: !message.expanded, displayText: message.expanded ? message.text.slice(0, 200) + "…" : message.text }) });
    },
    toggleReply() {
      const expanded = !this.data.replyExpanded;
      this.setData({ replyExpanded: expanded, replyText: expanded ? this.data.reply : this.data.reply.slice(0, 200) + "…" });
    },
    async reload() {
      const snapshot = this.syncIdentity(); if (!snapshot.owner || !this._visible) return;
      this.stopPolling(); const epoch = ++this._epoch;
      this.setData({ loading: true, busy: false, uploading: false, error: "" });
      try {
        if (this._pending && !await this.perform(this._pending, snapshot, epoch)) return;
        const threads = await agent.list(); if (!this.active(snapshot, epoch)) return;
        const choices = threads.filter((thread) => !this._semesterId || thread.semester_id === this._semesterId);
        let chosen = choices.find((thread) => this.data.thread && thread.id === this.data.thread.id) || choices[0] || null;
        // 初始化线程确认后可能刚关联学期；已有选择仍通过详情接口鉴权恢复。
        if (!chosen && this.data.thread && this.data.thread.id) chosen = this.data.thread;
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
        this.setData({ busy: false, needsReload: error.statusCode === 409, error: error.statusCode === 409 ? "对话或课表已更新。输入已保留，请重新读取并核对预览后再继续。" : `${error.message || "请求未完成。"}${rejected ? "" : " 结果尚未确认，请点击“重新读取”恢复，勿重复提交。"}` });
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
          const choice = await new Promise((resolve) => wx.showModal({ title: "识别课表截图", content: "图片和对话说明将通过服务器发送给 DeepSeek，用于整理你的课表。确认应用后删除图片，未确认的图片保留最多 7 天。请只选择需要识别的课表。", confirmText: "选择图片", success: resolve, fail: () => resolve({ confirm: false }) }));
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
    async decide(event) {
      if (this.data.busy || this.data.loading || this.data.uploading || this.data.needsReload || this._pending || !this.data.canConfirm) return;
      const snapshot = this.syncIdentity(); if (!snapshot.owner) return;
      const thread = this.data.thread; const decision = event.currentTarget.dataset.decision;
      if (!["apply", "cancel"].includes(decision)) return;
      return this.perform({ kind: "confirm", threadId: thread.id, runId: thread.run.id, data: { version: thread.version, decision } });
    },
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
      const semesterId = this._semesterId || (this.data.thread && this.data.thread.semester_id);
      this.stopPolling(); this._epoch += 1; this.setData({ text: "", images: [] });
      return this.perform({ kind: "create", data: semesterId ? { semester_id: semesterId } : {} }, snapshot, this._epoch);
    },
    goLogin() { this.closeAssistant(); wx.switchTab({ url: "/pages/settings/settings" }); },
    goTimetable() { this.closeAssistant(); },
  },
});
