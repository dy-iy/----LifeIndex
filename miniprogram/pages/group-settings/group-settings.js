const groups = require("../../services/groups");
const session = require("../../services/session");
const { avatarUrl } = require("../../services/backend");
const share = require("../../services/share");

const PREVIEW_LIMIT = 10;
const INVITE_REFRESH_MARGIN = 15000;
const INVITE_RETRY_DELAY = 15000;
const DIALOG_TITLES = { remove: "移除群聊成员", group: "群名称与头像",
  nickname: "我在本群的昵称", transfer: "转让群主" };

function memberRow(member, selectedIds, transferId) {
  const name = member.effective_name || member.display_name || "群成员";
  return { id: member.id, name, initial: Array.from(name)[0] || "群",
    avatarSrc: avatarUrl(member.avatar_url), isOwner: member.is_owner === true,
    isSelf: member.is_self === true, nickname: member.nickname || "",
    selected: selectedIds.indexOf(member.id) >= 0, transferSelected: member.id === transferId };
}

Page({
  data: {
    group: null, members: [], visibleMembers: [], removableMembers: [], transferMembers: [],
    showAllMembers: false, hasMoreMembers: false, loading: false, error: "",
    dialog: "", dialogTitle: "", busy: false, actionError: "", conflict: false,
    inviteReady: false, invitePreparing: false, inviteId: "", inviteError: "",
    nameDraft: "", nicknameDraft: "", selectedIds: [], transferId: "",
    leaveAfter: false, selectionCount: 0, avatarPending: false,
  },
  onLoad(options) {
    this._loadId = 0;
    this._viewEpoch = 0;
    this._visible = false;
    this._disposed = false;
    this._inviteEpoch = 0;
    this._shareInvites = [];
    this._groupId = options && typeof options.groupId === "string" ? options.groupId : "";
    this.unsubscribe = session.subscribe(() => {
      if (this._disposed) return;
      this._loadId += 1;
      this._viewEpoch += 1;
      this._fetchActive = null;
      this._snapshot = null;
      this.resetInvitation(true);
      this.clearGroup();
      if (this._visible) this.reload();
    });
  },
  onShow() {
    this._visible = true;
    this._viewEpoch += 1;
    this._goingBack = false;
    this._showingMember = false;
    this.setData({ busy: Boolean(this._pendingAction) });
    this.startRefresh();
    return this.reload().then(() => {
      if (this._autoUploadAvatar && this.ready() && this.data.dialog === "group" && this.data.group.canEdit) {
        this._autoUploadAvatar = false;
        this.setData({ avatarPending: true });
        return this.uploadChosenAvatar();
      }
    });
  },
  onHide() {
    this._visible = false;
    this._viewEpoch += 1;
    this._loadId += 1;
    this._fetchActive = null;
    this._inviteEpoch += 1;
    this._inviteOperation = null;
    this.stopRefresh();
    this.setData({ loading: false, inviteReady: false, invitePreparing: false });
  },
  onUnload() {
    this._disposed = true;
    this._visible = false;
    this._viewEpoch += 1;
    this._loadId += 1;
    this.resetInvitation(true);
    this.stopRefresh();
    if (this.unsubscribe) this.unsubscribe();
  },
  startRefresh() {
    this.stopRefresh();
    this._refreshTimer = setInterval(() => {
      if (this._visible && !this._disposed) {
        this.prepareInvitation();
        this.reload(true);
      }
    }, 5000);
  },
  stopRefresh() {
    if (this._refreshTimer) clearInterval(this._refreshTimer);
    this._refreshTimer = null;
  },
  clearGroup() {
    this._editorVersion = null;
    this._editorToken = null;
    this._avatarRetry = null;
    this._autoUploadAvatar = false;
    this.setData({ group: null, members: [], visibleMembers: [], removableMembers: [], transferMembers: [],
      showAllMembers: false, hasMoreMembers: false, loading: false, error: "", dialog: "", dialogTitle: "",
      inviteReady: false, invitePreparing: false, inviteId: "", inviteError: "",
      nameDraft: "", nicknameDraft: "", selectedIds: [], transferId: "",
      leaveAfter: false, selectionCount: 0, busy: false, actionError: "", conflict: false, avatarPending: false });
  },
  async reload(silent) {
    if (this._disposed || !this._visible || this._fetchActive) return;
    const snapshot = session.identity();
    this._snapshot = snapshot;
    const loadId = ++this._loadId;
    const operation = {};
    this._fetchActive = operation;
    const current = () => !this._disposed && this._visible && loadId === this._loadId
      && session.isCurrent(snapshot);
    if (!this._groupId || !snapshot.owner) {
      this._fetchActive = null;
      this.clearGroup();
      this.setData({ error: !this._groupId ? "群聊链接不完整，请返回列表重新打开。" : "请登录后查看群聊设置。" });
      return;
    }
    if (!silent && !this.data.group) this.setData({ loading: true, error: "" });
    try {
      const result = await groups.detail(this._groupId);
      if (!current()) return;
      const members = result.members.map((member) => memberRow(member, this.data.selectedIds, this.data.transferId));
      const self = members.find((member) => member.isSelf);
      const permissions = result.permissions || {};
      const group = { id: result.id, name: result.name, memberCount: result.member_count,
        maxMembers: result.max_members, version: result.version, isOwner: Boolean(self && self.isOwner),
        selfNickname: self ? self.name : "未设置", selfRawNickname: self ? self.nickname : "",
        avatarSrc: avatarUrl(result.avatar_url), initial: Array.from(result.name)[0] || "群",
        canInvite: permissions.can_invite === true, canEdit: permissions.can_edit === true,
        canRemove: permissions.can_remove === true, canTransfer: permissions.can_transfer === true,
        canDissolve: permissions.can_dissolve === true };
      const update = { group, members, error: "", loading: false, busy: Boolean(this._pendingAction),
        visibleMembers: this.data.showAllMembers ? members : members.slice(0, PREVIEW_LIMIT),
        removableMembers: members.filter((member) => !member.isOwner),
        transferMembers: members.filter((member) => !member.isSelf),
        hasMoreMembers: members.length > PREVIEW_LIMIT };
      const permissionLost = (this.data.dialog === "group" && !group.canEdit)
        || (this.data.dialog === "remove" && !group.canRemove)
        || (this.data.dialog === "transfer" && !group.canTransfer);
      if (permissionLost) {
        Object.assign(update, { dialog: "", selectedIds: [], selectionCount: 0, transferId: "" });
      }
      const previousMembers = this.data.members.map((member) => member.id).sort().join(",");
      if (!group.canInvite || (this.data.group && previousMembers !== members.map((member) => member.id).sort().join(","))) {
        this.resetInvitation(!group.canInvite);
        if (group.canInvite) this._minimumInviteVersion = group.version;
      }
      this.setData(update);
      this.prepareInvitation();
    } catch (error) {
      if (current()) {
        if (!this.handleAccessError(error)) this.setData({ error: error.message || "群聊设置加载失败，请重试。",
          busy: Boolean(this._pendingAction) });
      }
    } finally {
      if (this._fetchActive === operation) this._fetchActive = null;
      if (current()) this.setData({ loading: false });
    }
  },
  ready() {
    return !this._disposed && this._visible && !this._goingBack && this.data.group && session.isCurrent(this._snapshot);
  },
  currentContext() { return { snapshot: session.identity(), epoch: this._viewEpoch }; },
  contextCurrent(context) {
    return !this._disposed && this._visible && context.epoch === this._viewEpoch
      && session.isCurrent(context.snapshot);
  },
  handleAccessError(error) {
    if (error.statusCode === 404) {
      this.clearGroup();
      this.resetInvitation(true);
      this.stopRefresh();
      wx.showToast({ title: "该群聊已无法访问", icon: "none" });
      this.returnToList();
      return true;
    }
    if (error.statusCode === 401) {
      this.clearGroup();
      this.resetInvitation(true);
      this.setData({ error: error.message || "请重新登录后查看群聊。" });
      return true;
    }
    return false;
  },
  async runAction(action, success) {
    if (!this.ready() || this._pendingAction) return;
    const context = this.currentContext();
    const operation = {};
    this._pendingAction = operation;
    this.setData({ busy: true, actionError: "" });
    try {
      const result = await action();
      if (!this.contextCurrent(context)) return;
      // A read started before this commit must not overwrite the follow-up authoritative snapshot.
      this._loadId += 1;
      this._fetchActive = null;
      if (success) await success(result);
      if (this.contextCurrent(context) && this.data.group && !this._goingBack) await this.reload(true);
    } catch (error) {
      if (!this.contextCurrent(context)) return;
      if (this.handleAccessError(error)) return;
      this.setData({ actionError: error.statusCode === 409
        ? "群资料已发生变化，请关闭后重新打开此操作。" : error.message || "操作失败，请重试。",
        conflict: error.statusCode === 409 });
      if (!this.data.dialog) wx.showToast({ title: error.message || "操作失败，请重试", icon: "none" });
      if (error.statusCode === 403 || error.statusCode === 409) await this.reload(true);
    } finally {
      if (this._pendingAction === operation) this._pendingAction = null;
      if (this.contextCurrent(context)) this.setData({ busy: false });
    }
  },
  openDialog(dialog, extra) {
    if (!this.ready() || this._pendingAction) return false;
    this._editorVersion = this.data.group.version;
    this._editorToken = {};
    this.setData(Object.assign({ dialog, dialogTitle: DIALOG_TITLES[dialog], actionError: "", conflict: false }, extra || {}));
    return true;
  },
  closeDialog() {
    if (!this._disposed && !this._pendingAction) {
      this.setData({ dialog: "", actionError: "", conflict: false });
      this.prepareInvitation();
    }
  },
  stopPropagation() {},
  toggleMembers() {
    if (!this.ready() || !this.data.hasMoreMembers) return;
    const showAllMembers = !this.data.showAllMembers;
    this.setData({ showAllMembers,
      visibleMembers: showAllMembers ? this.data.members : this.data.members.slice(0, PREVIEW_LIMIT) });
  },
  showMember(event) {
    if (!this.ready() || this._showingMember) return;
    const member = this.data.members.find((item) => item.id === event.currentTarget.dataset.id);
    if (!member) return;
    this._showingMember = true;
    const roles = [];
    if (member.isOwner) roles.push("群主");
    if (member.isSelf) roles.push("我");
    wx.showModal({ title: "群聊成员", content: member.name + (roles.length ? "（" + roles.join(" · ") + "）" : ""),
      showCancel: false, confirmText: "知道了", complete: () => { this._showingMember = false; } });
  },
  avatarLoadError(event) {
    if (!this.ready()) return;
    const id = event.currentTarget.dataset.id;
    ["members", "visibleMembers", "removableMembers", "transferMembers"].forEach((key) => {
      const index = this.data[key].findIndex((member) => member.id === id);
      if (index >= 0) this.setData({ [key + "[" + index + "].avatarSrc"]: "" });
    });
  },
  groupAvatarLoadError() { if (this.ready()) this.setData({ "group.avatarSrc": "" }); },
  currentInvite() {
    return !this._inviteConsumed && this._invite && this._invite.context && session.isCurrent(this._invite.context.snapshot)
      && this.data.group && this.data.group.canInvite
      && Number.isFinite(Date.parse(this._invite.expires_at))
      && Date.parse(this._invite.expires_at) > Date.now() + INVITE_REFRESH_MARGIN;
  },
  resetInvitation(clearHistory) {
    this._inviteEpoch += 1;
    this._inviteOperation = null;
    this._invite = null;
    this._inviteConsumed = false;
    this._inviteRetryAt = 0;
    if (clearHistory) { this._shareInvites = []; this._minimumInviteVersion = 0; }
    if (!this._disposed) this.setData({ inviteReady: false, invitePreparing: false, inviteId: "", inviteError: "" });
  },
  async prepareInvitation(event) {
    if (!this.ready() || !this.data.group.canInvite) return;
    if (this._inviteConsumed && !this._inviteOperation) this.resetInvitation(false);
    if (this.currentInvite()) {
      this.setData({ inviteReady: true, inviteId: this._invite.id, inviteError: "" });
      return;
    }
    this.setData({ inviteReady: false });
    // Draft versions remain unchanged while an editor is already open.
    if (this.data.dialog || this._pendingAction || this._prompting || this._inviteOperation
      || (!event && Date.now() < (this._inviteRetryAt || 0))) return;
    const context = this.currentContext();
    const epoch = this._inviteEpoch;
    const operation = {};
    const startingVersion = this.data.group.version;
    this._inviteOperation = operation;
    this.setData({ invitePreparing: true, inviteError: "" });
    const current = () => this.contextCurrent(context) && epoch === this._inviteEpoch
      && this._inviteOperation === operation && this.data.group && this.data.group.canInvite;
    try {
      const result = await groups.createInvite(this._groupId);
      if (!current()) return;
      if (result.created_version <= (this._minimumInviteVersion || 0)) {
        throw new Error("群成员已变化，请重试邀请。");
      }
      if (Date.parse(result.expires_at) <= Date.now() + INVITE_REFRESH_MARGIN) {
        throw new Error("邀请已过期，请重试。");
      }
      this._invite = Object.assign({}, result, { context, groupName: this.data.group.name });
      this._shareInvites = (this._shareInvites || []).filter((invite) => invite.id !== result.id)
        .concat(this._invite).slice(-2);
      this._inviteRetryAt = 0;
      // Only our isolated invitation event may advance an editor opened during preparation.
      if (result.created_version === startingVersion + 1 && this.data.group.version === startingVersion) {
        if (this.data.dialog && this._editorVersion === startingVersion) this._editorVersion = result.created_version;
        this.setData({ "group.version": result.created_version });
      }
      this.setData({ inviteReady: true, inviteId: result.id, inviteError: "" });
      // Discard a detail request that began before our invitation committed.
      this._loadId += 1;
      this._fetchActive = null;
      this.reload(true);
    } catch (error) {
      if (!current()) return;
      if (this.handleAccessError(error)) return;
      this._inviteRetryAt = Date.now() + (error.statusCode === 429 ? 60000 : INVITE_RETRY_DELAY);
      this.setData({ inviteReady: false, inviteError: error.message || "邀请暂不可用，点击重试。" });
      if (error.statusCode === 403) this.reload(true);
    } finally {
      if (this._inviteOperation === operation) {
        this._inviteOperation = null;
        if (this.contextCurrent(context)) this.setData({ invitePreparing: false });
      }
    }
  },
  onShareAppMessage(event) {
    if (!event || event.from !== "button" || !event.target || !event.target.dataset
      || !event.target.dataset.groupInvite) return share.onShareAppMessage();
    const invitation = (this._shareInvites || []).find((invite) => invite.id === event.target.dataset.invitation
      && session.isCurrent(invite.context.snapshot));
    // The native button is disabled until a real credential is present. Retain the last
    // credentials for an already dispatched click; the join page checks expiry again.
    if (!invitation) { wx.showToast({ title: "邀请尚未就绪，请稍后重试", icon: "none" }); return; }
    const content = { title: "邀请你加入「" + (this.data.group ? this.data.group.name : invitation.groupName) + "」",
      path: "/pages/group-join/group-join?inviteToken=" + encodeURIComponent(invitation.token) };
    if (this._invite && this._invite.id === invitation.id && !this._inviteOperation) {
      this._inviteConsumed = true;
      this.setData({ inviteReady: false });
    }
    return content;
  },
  async confirmAction(title, content, confirmText) {
    if (!this.ready() || this._prompting || this._pendingAction || this.data.invitePreparing) return false;
    const context = this.currentContext();
    this._prompting = true;
    try {
      const response = await new Promise((resolve) => wx.showModal({ title, content,
        confirmText: confirmText || "确认", cancelText: "取消", success: resolve, fail: () => resolve({ confirm: false }) }));
      return response.confirm === true && this.contextCurrent(context) && this.ready();
    } finally { this._prompting = false; }
  },
  removeMembers() {
    if (!this.ready() || !this.data.group.canRemove) return;
    if (this.data.members.length < 2) { wx.showToast({ title: "暂无可移除的成员", icon: "none" }); return; }
    if (this.openDialog("remove", { selectedIds: [], selectionCount: 0 })) {
      this.setData({ removableMembers: this.data.members.filter((member) => !member.isOwner)
        .map((member) => Object.assign({}, member, { selected: false })) });
    }
  },
  selectMember(event) {
    if (!this.ready() || this._pendingAction || !this.data.group.canRemove || this.data.conflict) return;
    const id = event.currentTarget.dataset.id;
    if (!this.data.removableMembers.some((member) => member.id === id)) return;
    const selectedIds = this.data.selectedIds.indexOf(id) >= 0
      ? this.data.selectedIds.filter((value) => value !== id) : this.data.selectedIds.concat(id);
    if (selectedIds.length > 10) { wx.showToast({ title: "每次最多选择 10 位成员", icon: "none" }); return; }
    this.setData({ selectedIds, selectionCount: selectedIds.length,
      removableMembers: this.data.removableMembers.map((member) => Object.assign({}, member,
        { selected: selectedIds.indexOf(member.id) >= 0 })) });
  },
  async submitRemove() {
    if (!this.ready() || !this.data.group.canRemove || this.data.invitePreparing
      || this.data.conflict || !this.data.selectedIds.length) return;
    const ids = this.data.selectedIds.slice();
    const version = this._editorVersion;
    if (!await this.confirmAction("移除成员", "确认移除选中的 " + ids.length + " 位成员？他们将无法继续访问群聊。", "移除")) return;
    return this.runAction(() => groups.removeMembers(this._groupId, ids, version), (result) => {
      this.resetInvitation(false);
      this._minimumInviteVersion = result.version;
      this.setData({ dialog: "", selectedIds: [], selectionCount: 0 });
      wx.showToast({ title: "成员已移除", icon: "success" });
    });
  },
  editGroup() {
    if (!this.ready()) return;
    if (!this.data.group.canEdit) { wx.showToast({ title: "仅群主可修改群名称与头像", icon: "none" }); return; }
    if (this.openDialog("group", { nameDraft: this.data.group.name, avatarPending: false })) this._avatarRetry = null;
  },
  editNickname() {
    if (this.ready()) this.openDialog("nickname", { nicknameDraft: this.data.group.selfRawNickname });
  },
  updateNameDraft(event) {
    if (!this._pendingAction) this.setData({ nameDraft: event.detail.value, actionError: "" });
  },
  updateNicknameDraft(event) {
    if (!this._pendingAction) this.setData({ nicknameDraft: event.detail.value, actionError: "" });
  },
  submitGroup() {
    if (!this.ready() || !this.data.group.canEdit || this.data.invitePreparing || this.data.conflict) return;
    const name = this.data.nameDraft.trim();
    if (!name || Array.from(name).length > 60) {
      this.setData({ actionError: "群名称需为 1–60 个字。" }); return;
    }
    const version = this._editorVersion;
    return this.runAction(() => groups.updateGroup(this._groupId, name, version), () => {
      this.setData({ dialog: "" });
      wx.showToast({ title: "群资料已保存", icon: "success" });
    });
  },
  submitNickname() {
    if (!this.ready()) return;
    const nickname = this.data.nicknameDraft.trim();
    if (Array.from(nickname).length > 60) {
      this.setData({ actionError: "本群昵称最多 60 个字。" }); return;
    }
    return this.runAction(() => groups.updateNickname(this._groupId, nickname), () => {
      this.setData({ dialog: "" });
      wx.showToast({ title: "本群昵称已保存", icon: "success" });
    });
  },
  async chooseGroupAvatar() {
    if (!this.ready() || !this.data.group.canEdit || this.data.invitePreparing
      || this._pendingAction || this._choosingAvatar || this.data.conflict) return;
    const context = this.currentContext();
    const version = this._editorVersion;
    const editorToken = this._editorToken;
    this._choosingAvatar = true;
    try {
      const filePath = await new Promise((resolve, reject) => {
        if (wx.chooseMedia) wx.chooseMedia({ count: 1, mediaType: ["image"], sourceType: ["album", "camera"],
          success: (result) => resolve(result.tempFiles[0] && result.tempFiles[0].tempFilePath), fail: reject });
        else wx.chooseImage({ count: 1, sourceType: ["album", "camera"],
          success: (result) => resolve(result.tempFilePaths[0]), fail: reject });
      });
      // Native album/camera may hide and show the page; only retain the explicit selection for this editor/session.
      if (this._disposed || !session.isCurrent(context.snapshot) || this.data.dialog !== "group"
        || !this.data.group || !this.data.group.canEdit || editorToken !== this._editorToken
        || version !== this._editorVersion || !filePath) return;
      this._avatarRetry = { filePath, version };
      if (!this._visible) { this._autoUploadAvatar = true; return; }
      this.setData({ avatarPending: true });
      return this.uploadChosenAvatar();
    } catch (error) {
      if (this.contextCurrent(context) && !/cancel/.test(error.errMsg || "")) {
        this.setData({ actionError: "选择图片失败，请重试。" });
      }
    } finally { this._choosingAvatar = false; }
  },
  uploadChosenAvatar() {
    if (!this.ready() || !this.data.group.canEdit || this.data.invitePreparing || !this._avatarRetry || this.data.conflict) return;
    const pending = this._avatarRetry;
    return this.runAction(() => groups.uploadAvatar(this._groupId, pending.filePath, pending.version), (result) => {
      this._avatarRetry = null;
      this.setData({ avatarPending: false });
      // Adopt only our committed avatar version; a later concurrent change must still conflict with the name draft.
      this._editorVersion = result.version;
      wx.showToast({ title: "群头像已保存", icon: "success" });
    });
  },
  async leaveGroup() {
    if (!this.ready() || this._pendingAction || this.data.invitePreparing) return;
    if (this.data.group.isOwner) {
      if (this._prompting) return;
      const context = this.currentContext();
      this._prompting = true;
      let result;
      try {
        result = await new Promise((resolve) => wx.showActionSheet({ itemList: ["转让群主并退出", "仅转让群主", "解散群聊"],
          success: resolve, fail: () => resolve(null) }));
      } finally { this._prompting = false; }
      if (!result || !this.contextCurrent(context) || !this.ready()) return;
      if (result.tapIndex === 2) return this.dissolveGroup();
      return this.openTransfer(result.tapIndex === 0);
    }
    if (!await this.confirmAction("退出群聊", "退出「" + this.data.group.name + "」后，将无法继续访问群聊。", "退出")) return;
    return this.runAction(() => groups.leave(this._groupId), () => this.returnToList());
  },
  openTransfer(leaveAfter) {
    if (!this.ready() || !this.data.group.canTransfer) return;
    if (this.data.members.length < 2) {
      wx.showToast({ title: "群内暂无其他成员，可选择解散群聊", icon: "none" }); return;
    }
    if (this.openDialog("transfer", { transferId: "", leaveAfter })) {
      this.setData({ transferMembers: this.data.members.filter((member) => !member.isSelf)
        .map((member) => Object.assign({}, member, { transferSelected: false })) });
    }
  },
  selectTransferMember(event) {
    if (!this.ready() || this._pendingAction || !this.data.group.canTransfer || this.data.conflict) return;
    const id = event.currentTarget.dataset.id;
    if (!this.data.transferMembers.some((member) => member.id === id)) return;
    this.setData({ transferId: id, transferMembers: this.data.transferMembers
      .map((member) => Object.assign({}, member, { transferSelected: member.id === id })) });
  },
  async submitTransfer() {
    if (!this.ready() || !this.data.group.canTransfer || this.data.invitePreparing || !this.data.transferId || this.data.conflict) return;
    const member = this.data.transferMembers.find((item) => item.id === this.data.transferId);
    if (!member) return;
    const version = this._editorVersion;
    const leaveAfter = this.data.leaveAfter;
    if (!await this.confirmAction("转让群主", "确认将群主转让给「" + member.name + "」" + (leaveAfter ? "并退出群聊？" : "？"), "转让")) return;
    return this.runAction(() => groups.transferOwner(this._groupId, member.id, version, leaveAfter), () => {
      if (leaveAfter) this.returnToList();
      else { this.setData({ dialog: "" }); wx.showToast({ title: "群主已转让", icon: "success" }); }
    });
  },
  async dissolveGroup() {
    if (!this.ready() || !this.data.group.canDissolve || this.data.invitePreparing) return;
    const version = this.data.group.version;
    if (!await this.confirmAction("解散群聊", "解散「" + this.data.group.name + "」后，所有成员将无法继续访问，已发邀请全部失效。", "解散")) return;
    return this.runAction(() => groups.dissolve(this._groupId, version), () => this.returnToList());
  },
  async returnToList() {
    const context = this.currentContext();
    this._goingBack = true;
    this.resetInvitation(true);
    this.clearGroup();
    this.stopRefresh();
    let returned = false;
    try { returned = await groups.returnToList(); } catch (_) {}
    if (!returned && !this._disposed) {
      this._goingBack = false;
      if (this.contextCurrent(context)) this.setData({ error: "返回群聊列表失败，请重试。" });
    }
    return returned;
  },
  goBack() {
    if (this._disposed || this._goingBack || this._pendingAction) return;
    if (this.data.dialog) { this.closeDialog(); return; }
    this._goingBack = true;
    const fail = () => {
      if (this._disposed) return;
      this._goingBack = false;
      if (this._visible) wx.showToast({ title: "返回失败，请重试", icon: "none" });
    };
    if (getCurrentPages().length > 1) wx.navigateBack({ delta: 1, fail });
    else wx.redirectTo({ url: this._groupId ? "/pages/chat-room/chat-room?groupId=" + encodeURIComponent(this._groupId)
      : "/pages/chat/chat", fail });
  },
});
