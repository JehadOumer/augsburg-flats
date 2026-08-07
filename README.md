# Augsburg Flats (static GitHub Pages)

Mobile-first apartment gallery for Augsburg. Listings are scraped on your PC and published as JSON to GitHub Pages. Shortlist / hide sync across iPhone and iPad via a private GitHub Gist.

The older FastAPI app in `Project Accomidation` is unchanged — this is a separate project.

## Quick start

### 1. Create the GitHub repo

```bash
cd "c:\Users\Annir Member\Documents\Augsburg\augsburg-flats"
git init
git add .
git commit -m "Initial Augsburg Flats static site"
gh repo create augsburg-flats --public --source=. --remote=origin --push
```

Or create `augsburg-flats` on github.com, then:

```bash
git remote add origin https://github.com/YOUR_USER/augsburg-flats.git
git branch -M main
git push -u origin main
```

### 2. Enable GitHub Pages

1. Repo → **Settings → Pages**
2. Source: **GitHub Actions**
3. After the first push, the `Deploy GitHub Pages` workflow publishes the `site/` folder
4. Open `https://YOUR_USER.github.io/augsburg-flats/`

### 3. Desktop update shortcut

Run `Create-Desktop-Shortcut.bat` once (or right-click `Update-Listings.bat` → **Create shortcut** and move it to Desktop).

Double-click the shortcut twice a day (or whenever you want fresh listings).

The bat will: install deps → scrape → write `site/data/listings.json` → commit → push. Logs stay in the terminal window until you press a key.

First export without scraping (already seeded once):

```bash
python -m pipeline.export_listings --skip-scrape
```

### 4. Sync shortlist / hide (phone + iPad)

1. GitHub → **Settings → Developer settings → Personal access tokens**
2. Create a token with the **gist** scope only
3. On the site, tap **⚙**
4. Paste the token (leave Gist ID blank on the first device — it creates a private gist)
5. On the second device, paste the **same token and Gist ID**

Your PAT is stored only in that browser’s `localStorage`. Revoke it on GitHub if a device is lost.

## Project layout

```
augsburg-flats/
  site/                 # GitHub Pages content
    index.html
    css/ styles.css
    js/  app.js prefs.js
    data/listings.json  config.json
  pipeline/             # Local scrape + export only
  data/                 # Local SQLite (gitignored)
  Update-Listings.bat
```

## Notes

- Images load from listing sites directly (`referrerpolicy=no-referrer`). Some hosts may block hotlinking.
- Filters: price, photos, shortlist, term, tenancy, metro time, Sept 1, rented/gone, hidden.
- Prefer a **public** repo for free GitHub Pages, or use GitHub Pro for private Pages.
