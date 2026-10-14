const invitation = require("./services/group-invitation");

App({
  globalData: {},
  onLaunch(options) { invitation.capture(options); },
  onShow(options) { invitation.capture(options); },
});
