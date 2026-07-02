module.exports = {
  daemon: true,
  run: [
    {
      method: "shell.run",
      params: {
        path: "app",
        conda: {
          "path": "{{path.resolve(cwd, 'conda_env')}}"
        },
        env: {
          "PYTHONUNBUFFERED": "1"
        },
        message: [
          // Binds on every interface (LAN, Tailscale, loopback) at the family's
          // next fixed port so other devices and the sibling studios' clients
          // can reach the Hub directly. 47868-47872 are taken by the studios;
          // change here if 47873 clashes with something on your machine.
          "python -m uvicorn backend.main:app --host 0.0.0.0 --port 47873"
        ],
        on: [{
          event: "/Uvicorn running on (http:\\/\\/[0-9.:]+)/",
          done: true
        }, {
          event: "/error:/i",
          break: false
        }]
      }
    },
    {
      method: "local.set",
      params: {
        url: "{{input.event[1]}}"
      }
    }
  ]
}
