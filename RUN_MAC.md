# Run LableLens on macOS

## 1. Open the project folder

```bash
cd ~/Downloads/LableLens-v4
```

## 2. Activate your OCR environment

If you already use a shared environment:

```bash
source ~/.venv/bin/activate
```

Or create a project-local environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 3. Install/check dependencies

```bash
python -m pip install -r requirements.txt
```

## 4. Start LableLens

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

## 5. Check OCR routing

Open `http://localhost:8000/health`. The production mode should be `adaptive`. On a machine with RapidOCR installed, `rapidocr_available` should be `true`.

For an easy flat label, the result should normally show **Tesseract fast path**. If confidence or evidence is insufficient, the result should show **Tesseract → RapidOCR**.

## 6. Accuracy benchmark

```bash
python benchmark_ocr.py /path/to/product-photo.jpg
```

A safe optimization run ends with `ACCURACY PARITY: PASS`. If it reports `FAIL`, the full ensemble found a key value the adaptive route did not, so keep that image as a regression case and do not loosen the fast-path thresholds.

## 7. Temporary public preview

With LableLens running, open a second Terminal:

```bash
cloudflared tunnel --url http://localhost:8000
```

Use the generated `https://...trycloudflare.com` URL as a temporary trial link.


## Google Admin / Officer sign-in

1. In Google Cloud Console / Google Auth Platform, create an OAuth 2.0 Client ID of type **Web application**.
2. Add `http://localhost:8000` as an authorized JavaScript origin for local testing. Add the exact HTTPS origin used for your deployed site.
3. Export configuration before starting Uvicorn:

```bash
export LABLELENS_SECRET='replace-with-a-long-random-secret'
export GOOGLE_CLIENT_ID='YOUR_WEB_CLIENT_ID.apps.googleusercontent.com'
export GOOGLE_ADMIN_EMAILS='your-admin@gmail.com'
export GOOGLE_OFFICER_EMAILS='officer1@gmail.com,officer2@gmail.com'
```

Then start normally. Click the workspace/session menu and choose **Continue with Google**. The backend assigns the role from the allowlist.

For Cloudflare Quick Tunnels, the random hostname changes on each new tunnel. Google Sign-In requires the current hostname to be added as an authorized JavaScript origin. For a stable deployment, use a named Cloudflare Tunnel or a custom domain.
