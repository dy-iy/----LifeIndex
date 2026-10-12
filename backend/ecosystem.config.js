const path = require("node:path");

module.exports = {
  // API lifespan owns the background threads; run exactly one Uvicorn process.
  apps: [
    {
      name: "lifefund-backend",
      cwd: __dirname,
      script: path.join(__dirname, ".venv/bin/python"),
      interpreter: "none",
      args: [
        "-m", "uvicorn", "main:app",
        "--host", "127.0.0.1",
        "--port", "8003",
        "--proxy-headers",
        "--forwarded-allow-ips=127.0.0.1",
      ],
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      watch: false,
      min_uptime: "10s",
      max_restarts: 10,
      restart_delay: 3000,
      kill_timeout: 210000,
      time: true,
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};
