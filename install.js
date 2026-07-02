// Studio Hub runs NO local AI models (it only monitors the studios that do),
// so unlike the sibling studios there is no `requires: { bundle: "ai" }` and
// the environment is deliberately tiny: FastAPI + httpx + psutil.
module.exports = {
  run: [
    {
      method: "shell.run",
      params: {
        path: "app",
        conda: {
          "path": "{{path.resolve(cwd, 'conda_env')}}",
          "python": "python=3.12"
        },
        message: [
          "python -m pip install --upgrade pip",
          "uv pip install -r requirements.txt"
        ]
      }
    }
  ]
}
