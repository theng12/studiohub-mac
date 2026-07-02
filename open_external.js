// Open the dashboard in an external browser instead of Pinokio's embedded
// webview — same escape hatch the sibling studios ship (see imagestudio-mac).
// Called from pinokio.js like:
//   { href: "open_external.js", params: { url: "http://localhost:47873" } }
module.exports = {
  run: [{
    method: "web.open",
    params: {
      uri: "{{args.url}}",
      target: "_blank"
    }
  }]
}
