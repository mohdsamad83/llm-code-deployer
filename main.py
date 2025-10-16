import os
import re
import time
import base64
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
import requests
# Using the standard OpenAI client as required by your original code
from openai import OpenAI 
from github import Github, GithubException

# --- 1. SETUP AND CONFIGURATION ---

# Fetch your secret keys and username from the environment
MY_SECRET = os.getenv("MY_SECRET")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
# NOTE: This LLM_API_KEY should now be generated from aipipe.org
OPENAI_API_KEY = os.getenv("LLM_API_KEY") 
# ❗ IMPORTANT: This MUST be the correct username for Pages URL construction
GITHUB_USERNAME = "mohdsamad83" 

# --- NEW: AIPipe Configuration ---
# Set the base URL to AIPipe's OpenRouter-compatible endpoint
AIPipe_BASE_URL = "https://aipipe.org/openrouter/v1" 

# Check if all required secrets are set
if not all([MY_SECRET, GITHUB_TOKEN, OPENAI_API_KEY, GITHUB_USERNAME]):
    raise ValueError("One or more required environment variables or GITHUB_USERNAME are not set.")

# Initialize clients for the APIs we'll use
app = FastAPI()
# 🔑 CRITICAL CHANGE: Initialize OpenAI client with the AIPipe base_url
openai_client = OpenAI(api_key=OPENAI_API_KEY, base_url=AIPipe_BASE_URL)
github_client = Github(GITHUB_TOKEN)


## --- 2. DATA MODELS ---

# Defines the structure of the JSON request we expect to receive
class TaskRequest(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: list
    evaluation_url: str
    attachments: list = Field(default_factory=list)

# --- NEW: Helper to construct the unique repository name ---
def get_repo_name(task_id: str) -> str:
    """Returns the unique repository name based on the task ID."""
    # The requirement is to use a unique repo name based on .task.
    return f"llm-code-deployer-{task_id}"

## --- 3. HELPER FUNCTIONS (The Core Logic) ---

def generate_code_from_brief(brief: str, checks: list, existing_code: str = None) -> dict:
    """
    Calls the OpenAI API (proxied via AIPipe) to generate code/revision.
    If existing_code is provided, the prompt is tailored for revision (Round 2).
    """
    print(f"🤖 Calling OpenAI (via AIPipe) for {'revision' if existing_code else 'initial generation'}...")
    
    # --- MODEL SELECTION ---
    # Using the fully qualified model name for AIPipe/OpenRouter compatibility.
    MODEL_NAME = "openai/gpt-4o-mini"
    # --------------------------------------------------------------------------------
    # 🔑 KEY LOGIC: Prompt Engineering for Round 1 vs. Round 2
    # --------------------------------------------------------------------------------
    if existing_code:
        # Round 2: Instruct the LLM to modify the existing code
        prompt = f"""
        You are an expert web developer tasked with revising an existing single-page web application.

        The ORIGINAL CODE (index.html) to be REVISED is provided below:
        ---
        {existing_code}
        ---

        REVISION BRIEF: "{brief}"
        EVALUATION CHECKS: The final page must satisfy these new or updated requirements: {', '.join(checks)}.

        Your response MUST contain exactly two markdown code blocks: one for the new index.html and one for the revised README.md.
        Only include the full, complete, revised content for these two files. Do not include a LICENSE block.
        Do not write any other text, explanation, or introductions.

        Use this exact format for your output:
        ```html
        <!DOCTYPE html>
        ... (FULL REVISED HTML) ...
        ```

        ```markdown
        # Project Title - Revised
        ... (FULL REVISED README CONTENT) ...
        ```
        """
    else:
        # Round 1: Instruct the LLM to generate the initial files
        prompt = f"""
        You are an expert web developer creating a complete, self-contained single-page web application.

        BRIEF: "{brief}"
        EVALUATION CHECKS: The final page must satisfy these requirements: {', '.join(checks)}.

        Your response MUST contain exactly three markdown code blocks for the following files: index.html, README.md, and LICENSE.
        The LICENSE block must contain the full text of the MIT License.
        Do not write any other text, explanation, or introductions.

        Use this exact format for your output:
        ```html
        <!DOCTYPE html>
        <html lang="en">
        ...
        </html>
        ```

        ```markdown
        # Project Title
        A brief summary of the project.
        ```

        ```text
        MIT License
        ... (the rest of the full MIT license text) ...
        ```
        """
    # --------------------------------------------------------------------------------

    try:
        completion = openai_client.chat.completions.create(
            model=MODEL_NAME, # 🔑 CRITICAL CHANGE: Using the prefixed model name
            messages=[{"role": "user", "content": prompt}]
        )
        content = completion.choices[0].message.content
    except Exception as e:
        print(f"❌ OpenAI API call (via AIPipe) failed: {e}")
        raise

    # --------------------------------------------------------------------------------
    # 🔑 KEY LOGIC: Parsing for Round 1 vs. Round 2
    # --------------------------------------------------------------------------------
    html_match = re.search(r"```html\n(.*?)\n```", content, re.DOTALL)
    readme_match = re.search(r"```markdown\n(.*?)\n```", content, re.DOTALL)
    
    if not html_match or not readme_match:
        print("❌ Error: LLM response did not contain the required HTML and README blocks.")
        raise ValueError("Failed to parse LLM response. The output format was incorrect.")
    
    result = {
        "html": html_match.group(1).strip(),
        "readme": readme_match.group(1).strip(),
    }
    
    if not existing_code:
        # Only check for LICENSE in Round 1
        license_match = re.search(r"```text\n(.*?)\n```", content, re.DOTALL)
        if not license_match:
            print("❌ Error: LLM response did not contain the required LICENSE block for Round 1.")
            raise ValueError("Failed to parse LLM response. The output format was incorrect for Round 1.")
        result["license"] = license_match.group(1).strip()
    
    print("✅ Code generated/revised successfully.")
    return result

def create_and_deploy_repo(repo_name: str, files: dict) -> dict:
    """Creates a GitHub repo, uploads files, and constructs the Pages URL (used only for Round 1)."""
    print(f"🐙 Accessing GitHub to create repo: {repo_name}")
    user = github_client.get_user()
    
    try:
        # Create a new public repository.
        repo = user.create_repo(repo_name, private=False, auto_init=False)
        print(f"✅ Repo '{repo_name}' created.")
    except GithubException as e:
        # If the repo already exists, fail Round 1
        if e.status == 422:
            print(f"⚠️ Repo '{repo_name}' already exists. Failing Round 1 as expected.")
            raise HTTPException(status_code=409, detail=f"Repository {repo_name} already exists. Cannot complete Round 1.")
        else:
            print(f"❌ GitHub API error: {e}")
            raise

    # Upload the files generated by the LLM to the main branch
    repo.create_file("index.html", "feat: Initial application structure", files["html"], branch="main")
    repo.create_file("README.md", "docs: Add project README", files["readme"], branch="main")
    repo.create_file("LICENSE", "docs: Add MIT License", files["license"], branch="main")
    print("✅ Files committed to the repo.")

    # Giving GitHub Pages a moment to initialize the site build
    time.sleep(5) 
    
    # Construct the GitHub Pages URL based on the GITHUB_USERNAME defined in setup
    commit_sha = repo.get_branch("main").commit.sha
    pages_url = f"https://{user.login}.github.io/{repo.name}/"
    
    return {
        "repo_url": repo.html_url,
        "commit_sha": commit_sha,
        "pages_url": pages_url
    }

def update_and_redeploy_repo(repo_name: str, files: dict) -> dict:
    """Updates an EXISTING GitHub repo with new files (used only for Round 2)."""
    print(f"🔄 Starting Round 2 revision for repo: {repo_name}")
    user = github_client.get_user()
    
    try:
        repo = user.get_repo(repo_name)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Round 2 failed: Repository '{repo_name}' not found for revision.")

    # 1. Update index.html
    try:
        contents_html = repo.get_contents("index.html")
        repo.update_file(
            contents_html.path, 
            "feat: Round 2 code revision", 
            files["html"], 
            contents_html.sha, 
            branch="main"
        )
        print("✅ index.html updated.")
    except Exception as e:
        print(f"❌ Failed to update index.html: {e}")
        raise HTTPException(status_code=500, detail="Failed to update application code (index.html) in repo.")

    # 2. Update README.md
    try:
        contents_readme = repo.get_contents("README.md")
        repo.update_file(
            contents_readme.path, 
            "docs: Round 2 README update", 
            files["readme"], 
            contents_readme.sha, 
            branch="main"
        )
        print("✅ README.md updated.")
    except Exception as e:
        print(f"❌ Failed to update README.md: {e}")
        # This is less critical than the code, but still necessary.
        raise HTTPException(status_code=500, detail="Failed to update documentation (README.md) in repo.")

    # Wait for the commit to process before getting the SHA
    time.sleep(5)
    
    commit_sha = repo.get_branch("main").commit.sha
    pages_url = f"https://{user.login}.github.io/{repo.name}/"
    
    return {
        "repo_url": repo.html_url,
        "commit_sha": commit_sha,
        "pages_url": pages_url
    }

def notify_evaluation_server(url: str, payload: dict):
    """Sends the final results back to the instructor's server with retries."""
    print(f"📨 Notifying evaluation server for Round {payload.get('round')} at: {url}")
    
    # Retry with exponential backoff (1, 2, 4, 8 seconds)
    for i in range(4):
        try:
            response = requests.post(url, json=payload, timeout=20)
            if response.status_code == 200:
                print("✅ Successfully notified evaluation server.")
                return
            else:
                print(f"⚠️ Attempt {i+1} failed with status {response.status_code}. Retrying...")
        except requests.RequestException as e:
            print(f"⚠️ Attempt {i+1} failed with network error: {e}. Retrying...")
        
        time.sleep(2**i)
    
    print("❌ Failed to notify evaluation server after multiple retries.")
    # Re-raise an exception so the FastAPI background task logs it as a failure
    raise Exception("Failed to notify evaluation server.")


# --- New Function: Combined Background Processor ---
def process_task(request_data: TaskRequest):
    """The main workflow that runs in the background for either round."""
    repo_name = get_repo_name(request_data.task)
    print(f"🚀 Starting Round {request_data.round} processing for task: {repo_name}")
    
    try:
        if request_data.round == 1:
            # --- ROUND 1: CREATE ---
            generated_files = generate_code_from_brief(request_data.brief, request_data.checks)
            repo_details = create_and_deploy_repo(repo_name, generated_files)
        
        elif request_data.round == 2:
            # --- ROUND 2: REVISE ---
            # 1. Get existing code to provide context to the LLM
            user = github_client.get_user()
            repo = user.get_repo(repo_name)
            
            # Fetch the contents of the existing index.html
            existing_contents = repo.get_contents("index.html")
            # The content is base64 encoded, so it must be decoded
            existing_code = base64.b64decode(existing_contents.content).decode('utf-8')
            
            # 2. Generate the revised code and documentation
            generated_files = generate_code_from_brief(request_data.brief, request_data.checks, existing_code=existing_code)
            
            # 3. Update the existing repo
            repo_details = update_and_redeploy_repo(repo_name, generated_files)
        
        else:
            # Should be caught by the main endpoint, but good for safety.
            raise ValueError(f"Invalid round number: {request_data.round}")

        # --- FINAL STEP (COMMON TO BOTH ROUNDS) ---
        payload = {
            "email": request_data.email,
            "task": request_data.task,
            "round": request_data.round,
            "nonce": request_data.nonce,
            "repo_url": repo_details["repo_url"],
            "commit_sha": repo_details["commit_sha"],
            "pages_url": repo_details["pages_url"],
        }
        
        notify_evaluation_server(request_data.evaluation_url, payload)
        
    except Exception as e:
        # Log the critical failure, but allow the server to continue running.
        print(f"❌ An unrecoverable error occurred during Round {request_data.round} processing: {e}")
    
    print(f"🏁 Finished processing task: {request_data.task}")


## --- 4. API ENDPOINTS (The Server's "Doors") ---

@app.post("/api/deploy")
async def handle_deployment(request_data: TaskRequest, background_tasks: BackgroundTasks):
    """This is the main endpoint that receives requests from the instructor."""
    print(f"Received request for task: {request_data.task}, round: {request_data.round}")

    # Immediately verify the secret. If it's wrong, reject the request.
    if request_data.secret != MY_SECRET:
        print("❌ Secret mismatch. Aborting.")
        raise HTTPException(status_code=403, detail="Invalid secret provided.")

    # Check if the round is valid and add the task to the background
    if request_data.round in [1, 2]:
        # Crucially, we add the slow work as a background task.
        # This allows us to return a 200 OK response immediately, which is essential
        # for not blocking the external evaluation server.
        background_tasks.add_task(process_task, request_data)
        return {"status": "success", "message": f"Round {request_data.round} task accepted and is being processed in the background."}
    
    else:
        raise HTTPException(status_code=400, detail="Invalid round number specified (must be 1 or 2).")

@app.get("/")
def read_root():
    """A simple endpoint to confirm that the server is running."""
    return {"LLM Code Deployer API": "Online and ready!"}
