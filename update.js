// One-click Update — mode-aware (launchd service, start.js, or stopped). Pulls
// latest code, refreshes deps from source (installs new deps like python-multipart
// that the Hub needs to boot), and restarts the REAL server:
//   • service mode -> detect the loaded launchd label even if an older updater
//     lost its marker, then install_service.sh REWRITES the launchd plist to the
//     current on-disk scripts before relaunching (robust to the serve.sh ->
//     studiohub-serve.sh rename; a plain kickstart would relaunch a stale plist).
//   • otherwise -> start.js.
// Mutually exclusive, so a second server never fights the service for the port.
module.exports = {
  run: [
    {
      when: "{{running('start.js')}}",
      method: "script.stop",
      params: { uri: "{{path.resolve(cwd, 'start.js')}}" }
    },
    {
      when: "{{exists('.git')}}",
      method: "shell.run",
      params: { message: "git pull" }
    },
    {
      // launchd is authoritative. Also recognize an owned-but-untracked Hub
      // listener: older Pinokio state could forget start.js while leaving its
      // server alive. Recovering the marker lets install_service.sh safely take
      // over that listener instead of starting a second server on port 47873.
      when: "{{platform === 'darwin'}}",
      method: "shell.run",
      params: {
        message: "if launchctl print \"gui/$(id -u)/com.kh.studiohub.server\" >/dev/null 2>&1; then mkdir -p service && touch service/.installed; else ROOT=\"$PWD\"; for p in $(lsof -ti tcp:47873 -sTCP:LISTEN 2>/dev/null || true); do CMD=$(ps -p \"$p\" -o command= 2>/dev/null || true); CWD=$(lsof -a -p \"$p\" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1); if [[ \"$CMD\" == *\"$ROOT\"* || \"$CWD\" == \"$ROOT\" || \"$CWD\" == \"$ROOT/\"* ]]; then mkdir -p service && touch service/.installed; break; fi; done; fi"
      }
    },
    {
      when: "{{exists('conda_env')}}",
      method: "shell.run",
      params: {
        path: "app",
        conda: { "path": "{{path.resolve(cwd, 'conda_env')}}" },
        message: [
          "python -m pip install --upgrade pip",
          "uv pip install -r requirements.lock"
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
