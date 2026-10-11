const createReviewController = require("../../services/review-controller");
const controller = createReviewController();
const { data, ...methods } = controller;

Component({
  options: { styleIsolation: "apply-shared" },
  properties: {
    sessionId: { type: String, value: "" },
    visible: { type: Boolean, value: true },
  },
  data,
  observers: {
    "visible, sessionId": function () {
      if (!this._attached) return;
      this.syncVisibility();
    },
  },
  lifetimes: {
    attached() { this._attached = true; this._pageVisible = true; this.onLoad(); },
    ready() { this.syncVisibility(); },
    detached() { this._attached = false; this.onUnload(); },
  },
  pageLifetimes: {
    show() { this._pageVisible = true; this.syncVisibility(); },
    hide() { this._pageVisible = false; this.syncVisibility(); },
  },
  methods: Object.assign({}, methods, {
    syncVisibility() {
      if (!this._attached || this._disposed) return;
      const visible = this.data.visible && this._pageVisible;
      const target = this.data.sessionId;
      if (visible && (!this._visible || this._shownTarget !== target)) {
        this._shownTarget = target;
        return this.onShow();
      }
      if (!visible && this._visible) this.onHide();
    },
  }),
});
