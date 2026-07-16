# WhatsApp bot — MBE (Mail Boxes Etc)

Bot de WhatsApp Cloud API directo (sin Chatwoot). Consulta de paquetes contra
un Google Sheet publicado como CSV.

## 1. Crear el repo en GitHub

```bash
cd whatsapp-mbe-bot
git init
git add .
git commit -m "Initial commit"
gh repo create quai-analytics/whatsapp-mbe-bot --private --source=. --push
# o crea el repo manualmente en github.com y:
#   git remote add origin <url>
#   git push -u origin main
```

## 2. Crear los secretos en Secret Manager (una sola vez)

```bash
gcloud config set project whatsapp-agent-mbe

printf '%s' 'TU_WHATSAPP_TOKEN' | gcloud secrets create WHATSAPP_TOKEN --data-file=-
printf '%s' 'TU_GEMINI_API_KEY' | gcloud secrets create GEMINI_API_KEY --data-file=-
```

## 3. Primer despliegue a Cloud Run (manual, define los env vars)

```bash
gcloud run deploy whatsapp-mbe-bot \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars WHATSAPP_PHONE_NUMBER_ID=...,WHATSAPP_VERIFY_TOKEN=mbe-verify-2026,WHATSAPP_API_VERSION=v22.0,MBE_SHEET_CSV_URL=...,CHATWOOT=false \
  --set-secrets WHATSAPP_TOKEN=WHATSAPP_TOKEN:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest
```

Cloud Run conserva estos env vars/secretos en despliegues futuros — no hace
falta repetirlos en el CI/CD.

## 4. Conectar GitHub a Cloud Build (consola, requiere OAuth — no se puede scriptear)

1. Ve a [Cloud Build > Triggers](https://console.cloud.google.com/cloud-build/triggers) en el proyecto `whatsapp-agent-mbe`.
2. **Conectar repositorio** → GitHub → autoriza la app de Cloud Build → elige `whatsapp-mbe-bot`.
3. **Crear un trigger**:
   - Evento: Push a una rama
   - Rama: `^main$`
   - Configuración: Cloud Build configuration file → `cloudbuild.yaml`
4. Guarda.

## 5. Dar permisos a la cuenta de servicio de Cloud Build

La SA por defecto de Cloud Build (Compute Engine default SA) necesita poder
desplegar en Cloud Run y actuar como la SA de runtime:

```bash
PROJECT_NUM=$(gcloud projects describe whatsapp-agent-mbe --format="value(projectNumber)")
SA="${PROJECT_NUM}-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding whatsapp-agent-mbe \
  --member="serviceAccount:${SA}" --role="roles/run.admin"
gcloud projects add-iam-policy-binding whatsapp-agent-mbe \
  --member="serviceAccount:${SA}" --role="roles/iam.serviceAccountUser"
```

(`roles/storage.objectViewer`, `roles/logging.logWriter` y
`roles/artifactregistry.writer` ya deberían estar otorgadas — se
concedieron durante la configuración inicial del proyecto.)

## 6. Listo

Cada `git push` a `main` reconstruye la imagen y actualiza el servicio.
Toma la URL con:

```bash
gcloud run services describe whatsapp-mbe-bot --region us-central1 --format="value(status.url)"
```

Regístrala como webhook en el panel de Meta: `<url>/webhook`, con el mismo
`WHATSAPP_VERIFY_TOKEN` que pusiste en el paso 3.
