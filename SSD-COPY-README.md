# Copying models to your Macs with the SSD

Downloading models on every Mac is slow and sometimes fails. This copies them
from the SSD instead, and clears out models a Mac is too small to run.

You do **Part 1 once**, on your main computer. Then **Part 2 at each Mac**.

Every command below can be pasted straight into Terminal.

---

## Part 1 — once, on your main computer

**Plug the SSD in first.**

### 1. See what will be copied (this changes nothing)

```bash
cd ~/pinokio/api/studiohub-mac*/ && python3 tools/studio_models.py stage --plan
```

You should see roughly **50 GB** — about 28 GB of voice models and 23 GB of
image models. If it says a studio is "not reachable", open that studio in
Pinokio so it is running, then run the command again.

### 2. Copy them to the SSD

```bash
cd ~/pinokio/api/studiohub-mac*/ && python3 tools/studio_models.py stage
```

This takes a while. It prints each model as it copies. When it finishes it says
how many GB it wrote.

### 3. Put this guide on the SSD too

```bash
cp ~/pinokio/api/studiohub-mac*/SSD-COPY-README.md /Volumes/UGREEN-1TB/READ-ME-FIRST.md
```

Now you can read it at each Mac without needing this chat.

---

## Part 2 — at each Mac

Plug the SSD in. Open Terminal. Run these **three** commands in order.

### 1. Get the latest tools

```bash
cd ~/pinokio/api/studiohub-mac*/ && git pull
```

### 2. See what it plans to do (this changes nothing)

```bash
cd ~/pinokio/api/studiohub-mac*/ && python3 tools/studio_models.py restore --plan --prune
```

Read the summary at the bottom. It tells you how many models it will add and
how many it will delete. Nothing has happened yet.

### 3. Do it

```bash
cd ~/pinokio/api/studiohub-mac*/ && python3 tools/studio_models.py restore --prune
```

When it finishes, **restart Voice Studio and Image Studio** in Pinokio so they
notice the new models.

That's it. Move to the next Mac.

---

## What it is actually doing

- It checks how much memory that Mac has, and only installs models that Mac can
  actually run. You do not tell it anything — it works this out itself.
- It skips models that are already there and complete, so running it twice is
  safe and the second run is fast.
- If a model is there but damaged, it replaces it.
- `--prune` deletes models that Mac cannot use — around **90 GB** on a machine
  that has been collecting them, counting both models dropped from the catalogue
  and models the Mac is too small to run.
- It also skips and removes voice models that **cannot clone a voice**, since
  the fleet exists to clone your own voices. Kokoro is kept anyway because it is
  tiny and handy for quick narration.

### What it will never delete

- Anything a Mac can actually run and that can clone.
- Kokoro, or the speech-to-text model used to check the others.
- Shared parts that models you are keeping still depend on.
- Anything whose memory requirement has not been measured yet.

If you want the non-cloning ones kept after all, add `--keep-non-cloning` to any
command.

If you would rather not delete anything, just leave `--prune` off:

```bash
cd ~/pinokio/api/studiohub-mac*/ && python3 tools/studio_models.py restore
```

---

## If something looks wrong

**"no MANIFEST.json — is the SSD plugged in?"**
The SSD is not mounted, or Part 1 was never finished. Check the SSD appears in
Finder.

**"cannot store symlinks"**
The SSD is formatted as exFAT or FAT32. It must be APFS, otherwise the copy
would silently double in size. Reformatting erases the drive, so ask me first.

**"Operation not permitted"**
macOS is blocking Terminal from the SSD. Go to System Settings → Privacy &
Security → Files and Folders, find Terminal, and turn on Removable Volumes.

**"not reachable on :47870" or ":47868"**
That studio is not running on this Mac. Open it in Pinokio, then re-run. If that
Mac genuinely does not have that studio installed, this is fine — it just skips
it.

**A studio is "serving an older build than the code on disk"**
Restart that studio before doing Part 1. Otherwise it copies out-of-date
information about which models fit which machines.
