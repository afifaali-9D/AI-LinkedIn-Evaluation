# Merged Langfuse Project — LinkedIn LLM + Femverse LLM
 
## Folder Structure
 
```
merged-langfuse/
├── .env                        ← Single env file (both projects' keys)
├── docker-compose.yml          ← One Compose file, three services
├── Dockerfile.linkedin         ← Builds the LinkedIn Streamlit app
├── Dockerfile.femverse         ← Builds the Femverse evaluation app
├── requirements.txt            ← Shared Python deps
├── shared/
│   └── langfuse_client.py      ← Shared Langfuse init helper
├── linkedin/
│   ├── ui_app.py               ← (copy from AI LinkedIn Manager)
│   └── post_creator_end_point.py
└── femverse/
    └── femverse-evaluation.py  ← (copy from femverse-evaluation)
```
 
## Setup
 
### 1. Fill in your `.env`
Open `.env` and replace the placeholder keys with your real ones from
the Langfuse dashboard (Settings → API Keys for each project).
 
```
LINKEDIN_LANGFUSE_PUBLIC_KEY=pk-lf-...
LINKEDIN_LANGFUSE_SECRET_KEY=sk-lf-...
 
FEMVERSE_LANGFUSE_PUBLIC_KEY=pk-lf-...
FEMVERSE_LANGFUSE_SECRET_KEY=sk-lf-...
```
 
### 2. Copy your existing Python files
 
```bash
cp /path/to/AI-LinkedIn-Evaluation/ui_app.py          linkedin/ui_app.py
cp /path/to/AI-LinkedIn-Evaluation/post_creator_end_point.py linkedin/
cp /path/to/femverse-evaluation/femverse-evaluation.py femverse/
```
 
### 3. Update imports in your Python files
In each Python file, replace manual Langfuse init with the shared helper:
 
```python
# Before (old, per-project)
from langfuse import Langfuse
langfuse = Langfuse(public_key="...", secret_key="...", host="...")
 
# After (new, shared helper)
import sys; sys.path.append('/app')
from shared.langfuse_client import get_langfuse_client
langfuse = get_langfuse_client()
```
 
### 4. Start everything
 
```bash
# Start all services (Langfuse server + both apps)
docker compose up -d
 
# Or start only one app + the shared server
docker compose up -d langfuse-server linkedin-app
docker compose up -d langfuse-server femverse-app
```
 
### 5. Access
 
| Service         | URL                    |
|----------------|------------------------|
| Langfuse UI     | http://localhost:3000  |
| LinkedIn App    | http://localhost:8501  |
| Femverse App    | http://localhost:8502  |
 
## How the keys work
 
Docker Compose maps each project's specific keys to the generic
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` env-vars **per service**:
 
```yaml
linkedin-app:
  environment:
    LANGFUSE_PUBLIC_KEY: ${LINKEDIN_LANGFUSE_PUBLIC_KEY}
    LANGFUSE_SECRET_KEY: ${LINKEDIN_LANGFUSE_SECRET_KEY}
 
femverse-app:
  environment:
    LANGFUSE_PUBLIC_KEY: ${FEMVERSE_LANGFUSE_PUBLIC_KEY}
    LANGFUSE_SECRET_KEY: ${FEMVERSE_LANGFUSE_SECRET_KEY}
```
 
So each app only ever sees its own project's keys — no mixing.