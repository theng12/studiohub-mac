module.exports = {
  version: "3.6",
  title: "Studio Hub KH",
  description: "Control plane for the KH Studio family — live health, unified model catalog, and unified-memory monitoring for Image/Music/Voice/Chat/Video Studio.",
  icon: "icon.png",
  menu: async (kernel, info) => {
    const installed = info.exists("conda_env")
    const running = {
      install: info.running("install.js"),
      start: info.running("start.js"),
      update: info.running("update.js"),
      reset: info.running("reset.js")
    }

    if (running.install) {
      return [{
        default: true,
        icon: "fa-solid fa-plug",
        text: "Installing",
        href: "install.js"
      }]
    }
    if (running.update) {
      return [{
        default: true,
        icon: "fa-solid fa-rotate",
        text: "Updating",
        href: "update.js"
      }]
    }
    if (running.reset) {
      return [{
        default: true,
        icon: "fa-solid fa-broom",
        text: "Resetting",
        href: "reset.js"
      }]
    }

    if (!installed) {
      return [{
        default: true,
        icon: "fa-solid fa-plug",
        text: "Install",
        href: "install.js"
      }]
    }

    if (running.start) {
      const local = info.local("start.js")
      if (local && local.url) {
        // Cache-bust so Pinokio's embedded webview can't serve a stale build
        // (same convention as the sibling studios).
        const cb = Date.now()
        // Browser-friendly URL: replace 0.0.0.0 (server-bind) with localhost
        // (client-reachable) so browsers can actually connect.
        const browserUrl = local.url.replace("0.0.0.0", "localhost")
        const portMatch = local.url.match(/:(\d+)/)
        const port = portMatch ? portMatch[1] : "?"
        return [
          {
            default: true,
            icon: "fa-solid fa-satellite-dish",
            text: "Open Dashboard",
            href: `${local.url}/?_cb=${cb}`
          },
          {
            icon: "fa-solid fa-arrow-up-right-from-square",
            text: `Port ${port} · Open in Browser`,
            href: "open_external.js",
            params: { url: browserUrl }
          },
          {
            icon: "fa-solid fa-terminal",
            text: "Terminal",
            href: "start.js"
          },
          {
            icon: "fa-solid fa-rotate",
            text: "Update",
            href: "update.js"
          }
        ]
      }
      return [{
        default: true,
        icon: "fa-solid fa-terminal",
        text: "Terminal",
        href: "start.js"
      }]
    }

    return [
      {
        default: true,
        icon: "fa-solid fa-power-off",
        text: "Start",
        href: "start.js"
      },
      {
        icon: "fa-solid fa-rotate",
        text: "Update",
        href: "update.js"
      },
      {
        icon: "fa-solid fa-plug",
        text: "Reinstall",
        href: "install.js"
      },
      {
        icon: "fa-regular fa-circle-xmark",
        text: "Reset",
        href: "reset.js"
      }
    ]
  }
}
