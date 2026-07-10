// One-click Update — mode-aware (launchd service, start.js, or stopped). Pulls
// latest code, refreshes deps from source (installs new deps like python-multipart
// that the Hub needs to boot), and restarts the REAL server:
//   • service mode -> install_service.sh, which REWRITES the launchd plist to the
//     current on-disk scripts before relaunching (robust to the serve.sh ->
//     studiohub-serve.sh rename; a plain kickstart would relaunch a stale plist).
//   • otherwise -> start.js.
// Mutually exclusive, so a second server never fights the service for the port.
module.exports = {
  run: [
    {
      when: "{{running('start.js')}}",
      method: "script.stop",
      params: { uri: "start.js" }
    },
    {
      when: "{{exists('.git')}}",
      method: "shell.run",
      params: { message: "git pull" }
    },
    {
      when: "{{exists('conda_env')}}",
      method: "shell.run",
      params: {
        path: "app",
        conda: { "path": "{{path.resolve(cwd, 'conda_env')}}" },
        message: [
          "python -m pip install --upgrade pip",
          "uv pip install -r requirements.txt"
        ]
      }
    },
    {
      when: "{{exists('service/.installed')}}",
      method: "shell.run",
      params: { message: [ "bash install_service.sh" ] }
    },
    {
      when: "{{!exists('service/.installed')}}",
      method: "script.start",
      params: { uri: "start.js" }
    },
    {
      method: "notify",
      params: { html: "Updated &amp; restarted — you're on the latest version." }
    }
  ]
}
