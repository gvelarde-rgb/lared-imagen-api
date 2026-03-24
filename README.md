# La Red - API de Generación de Imágenes

API para generar imágenes de noticias con el diseño de La Red 106.1.

## Endpoint

### `POST /generar-imagen`

**Body (JSON):**
```json
{
  "foto_url": "https://url-de-la-foto.jpg",
  "titulo": "Título de la noticia"
}
```

**Respuesta:**
```json
{
  "imagen_url": "https://res.cloudinary.com/dd9cuovet/image/upload/...",
  "ok": true
}
```

## Variables de entorno requeridas

| Variable | Descripción |
|---|---|
| `CLOUDINARY_CLOUD_NAME` | Cloud name de Cloudinary |
| `CLOUDINARY_API_KEY` | API Key de Cloudinary |
| `CLOUDINARY_API_SECRET` | API Secret de Cloudinary |

## Despliegue en Render.com

1. Subir este repositorio a GitHub
2. Crear nuevo Web Service en Render
3. Conectar el repositorio
4. Configurar las variables de entorno
5. Render detectará el Dockerfile automáticamente
