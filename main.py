import os
import re
import time
import base64
import mimetypes
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
import requests
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
class Attachment(BaseModel):
    name: str
    url: str

class TaskRequest(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: list
    evaluation_url: str
    # Updated to use the new Attachment model for clarity and validation
    attachments: list[Attachment] = Field(default_factory=list)

# --- NEW: Helper to construct the unique repository name ---
def get_repo_name(task_id: str) -> str:
    """Returns the unique repository name based on the task ID."""
    return f"llm-code-deployer-{task_id}"


## --- 3. HELPER FUNCTIONS (The Core Logic) ---

def get_attachment_context(attachments: list[Attachment]) -> tuple[list, str]:
    """
    Decodes attachments and prepares them for the LLM.
    Returns: A list of message content blocks (for vision) and a string of text context.
    """
    llm_content_blocks = []
    text_context = ""

    for attachment in attachments:
        # Data URLs follow the format: data:<mediatype>[;base64],<data>
        match = re.match(r"data:(.*?)(;base64)?,(.*)", attachment.url, re.DOTALL)
        if not match:
            print(f"⚠️ Skipping attachment: {attachment.name}. URL format is invalid.")
            continue
            
        mime_type = match.group(1)
        is_base64 = match.group(2)
        encoded_data = match.group(3)

        if not is_base64:
            print(f"⚠️ Skipping attachment: {attachment.name}. Data is not Base64 encoded.")
            continue

        try:
            # Prepare the Base64 prefix for the LLM image block
            base64_data_prefix = f"data:{mime_type}{is_base64},{encoded_data}"

            # --- Handle Image Attachments (Vision Model) ---
            if mime_type.startswith('image/'):
                print(f"🖼️ Found image attachment: {attachment.name}. Adding as vision input.")
                llm_content_blocks.append({
                    "type": "image_url",
                    "image_url": {"url": base64_data_prefix}
                })
                text_context += f"The user has provided an image named '{attachment.name}' for visual reference. You must follow instructions related to this image."

            # --- Handle Text/Data Attachments (CSV, Markdown, JSON) ---
            elif mime_type.startswith('text/') or mime_type.endswith('/json') or mime_type.endswith('/csv'):
                print(f"📄 Found text/data attachment: {attachment.name}. Decoding content.")
                decoded_content = base64.b64decode(encoded_data).decode('utf-8')
                text_context += (
                    f"\n---\nFILE: {attachment.name} ({mime_type})\n"
                    f"CONTENT:\n{decoded_content}\n---\n"
                )

            # --- Ignore Other Attachments (e.g., binaries) ---
            else:
                print(f"⚠️ Ignoring unsupported attachment type: {mime_type} for file {attachment.name}.")

        except Exception as e:
            print(f"❌ Failed to process attachment {attachment.name}: {e}")
            continue

    return llm_content_blocks, text_context


def generate_code_from_brief(
    brief: str, 
    checks: list, 
    attachment_blocks: list, 
    attachment_text_context: str, 
    existing_code: str = None
) -> dict:
    """
    Calls the OpenAI API (proxied via AIPipe) to generate code/revision.
    Includes explicit instructions for using attachments and strict adherence to the output format.
    """
    print(f"🤖 Calling OpenAI (via AIPipe) for {'revision' if existing_code else 'initial generation'}...")
    
    # --- MODEL SELECTION ---
    # Using the multi-modal model to handle both image (vision) and text attachments.
    MODEL_NAME = "openai/gpt-4o-mini" 
    
    # --- Base Instruction Template (Hardened) ---
    base_instruction = f"""
    You are an expert, highly precise, and efficient web developer. Your goal is to write a single-page, self-contained web application (HTML/CSS/JS) that perfectly meets the user's requirements.

    CRITICAL RULES:
    1. STRICT FORMAT: Your response MUST contain ONLY the required markdown code blocks and nothing else.
    2. SINGLE FILE: The application logic MUST be self-contained within the <script> tags of index.html. Do not create separate .js or .css files.
    3. ATTACHMENT USE: If data files (CSV, JSON, MD, etc.) are provided below, your JavaScript code MUST load and process them (using `fetch(filename)`) as part of the app logic. If an image is provided, generate code based on the image's appearance or content as requested in the brief.

    BRIEF: "{brief}"
    EVALUATION CHECKS: The final page must satisfy these functional requirements: {', '.join(checks)}.

    --- ATTACHMENT DATA CONTEXT ---
    {attachment_text_context if attachment_text_context else "No text/data files were provided."}
    --- END CONTEXT ---
    """
    
    if existing_code:
        # Round 2: Instruct the LLM to modify the existing code
        prompt_text = base_instruction + f"""
        
        TASK MODE: REVISION (Round 2)
        The ORIGINAL CODE (index.html) to be REVISED is provided below. You MUST read this code to apply the revisions correctly.
        ---
        {existing_code}
        ---

        Your response MUST contain exactly two markdown code blocks: one for the **new index.html** and one for the **revised README.md**.
        Do not include a LICENSE block.

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
        prompt_text = base_instruction + f"""
        
        TASK MODE: CREATION (Round 1)
        
        Your response MUST contain exactly three markdown code blocks for the following files: **index.html**, **README.md**, and **LICENSE**.
        The LICENSE block must contain the full text of the MIT License, which is publicly available.

        Use this exact format for your output:
        ```html
        <!DOCTYPE html>
        <html lang="en">
        ...
        </html>
        ```

        ```markdown
        # Project Title
        A brief summary of the project. Include setup, usage, code explanation, and license mention.
        ```

        ```text
        MIT License
        ... (the rest of the full MIT license text) ...
        ```
        """

    # --- Construct the messages list for the LLM (Vision/Text) ---
    content_blocks = [{"type": "text", "text": prompt_text}]
    # The message sent to the API is a list containing image objects and the final text prompt
    final_message_content = attachment_blocks + content_blocks

    try:
        completion = openai_client.chat.completions.create(
            model=MODEL_NAME, 
            messages=[{"role": "user", "content": final_message_content}]
        )
        content = completion.choices[0].message.content
    except Exception as e:
        print(f"❌ OpenAI API call (via AIPipe) failed: {e}")
        raise

    # ... (Rest of the file parsing logic remains the same) ...
    # This logic is robust and relies on the LLM following the strict format.
    html_match = re.search(r"```html\n(.*?)\n```", content, re.DOTALL)
    readme_match = re.search(r"```markdown\n(.*?)\n```", content, re.DOTALL)
    
    # ... (Final checks and return) ...
    
    # Rest of your existing parsing logic goes here...
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
    
    # --- CRITICAL: Add the attachment files to the repo if they are text/data files ---
    # The image logic is inside the LLM prompt. For CSV/JSON, they must be in the repo
    # so the generated index.html can load them.
    for attachment in files.get("attachments_to_commit", []):
        try:
            # The 'url' contains the data: MIME;base64, content
            match = re.match(r"data:.*?base64,(.*)", attachment["url"], re.DOTALL)
            if match:
                decoded_content = base64.b64decode(match.group(1)).decode('utf-8')
                repo.create_file(attachment["name"], f"data: Add {attachment['name']}", decoded_content, branch="main")
                print(f"✅ Data file {attachment['name']} committed to the repo.")
            else:
                print(f"⚠️ Could not parse data URL for file {attachment['name']}. Skipping commit.")
        except Exception as e:
            print(f"❌ Failed to commit data file {attachment['name']}: {e}")

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

    # List of files we intend to commit (path, content, commit_message, sha)
    files_to_commit = []

    # 1. Update index.html
    try:
        contents_html = repo.get_contents("index.html")
        files_to_commit.append({
            "path": contents_html.path, 
            "message": "feat: Round 2 code revision", 
            "content": files["html"], 
            "sha": contents_html.sha,
        })
        print("✅ index.html staged for update.")
    except Exception as e:
        print(f"❌ Failed to get index.html for update: {e}")
        raise HTTPException(status_code=500, detail="Failed to stage application code (index.html) in repo.")

    # 2. Update README.md
    try:
        contents_readme = repo.get_contents("README.md")
        files_to_commit.append({
            "path": contents_readme.path, 
            "message": "docs: Round 2 README update", 
            "content": files["readme"], 
            "sha": contents_readme.sha,
        })
        print("✅ README.md staged for update.")
    except Exception as e:
        print(f"❌ Failed to get README.md for update: {e}")
        # This is less critical than the code, but still necessary.
        raise HTTPException(status_code=500, detail="Failed to stage documentation (README.md) in repo.")

    # 3. Handle NEW attachment files (like a new JSON/CSV provided in Round 2)
    for attachment in files.get("attachments_to_commit", []):
        try:
            match = re.match(r"data:.*?base64,(.*)", attachment["url"], re.DOTALL)
            if match:
                decoded_content = base64.b64decode(match.group(1)).decode('utf-8')
                
                # Check if the file already exists (Round 2 could include a revision to an attachment)
                try:
                    existing_content = repo.get_contents(attachment["name"])
                    files_to_commit.append({
                        "path": existing_content.path,
                        "message": f"data: Update {attachment['name']} for Round 2",
                        "content": decoded_content,
                        "sha": existing_content.sha,
                    })
                    print(f"✅ Existing data file {attachment['name']} staged for update.")
                except GithubException as e:
                    # File not found (404), so create it
                    if e.status == 404:
                         repo.create_file(attachment["name"], f"data: Add {attachment['name']} for Round 2", decoded_content, branch="main")
                         print(f"✅ New data file {attachment['name']} committed to the repo.")
                    else:
                        raise e # Re-raise other GitHub errors
        except Exception as e:
            print(f"❌ Failed to stage/commit data file {attachment['name']}: {e}")
    
    # Commit all staged changes
    commit_sha = ""
    for file_data in files_to_commit:
        repo.update_file(
            file_data["path"],
            file_data["message"],
            file_data["content"],
            file_data["sha"],
            branch="main"
        )
        print(f"✅ Committed: {file_data['path']}")
        
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
    """Sends the final results back to the instructor's server with retries (Exponential Backoff)."""
    print(f"📨 Notifying evaluation server for Round {payload.get('round')} at: {url}")
    
    # Retry with exponential backoff (1, 2, 4, 8 seconds) up to 4 times
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
        
        # Exponential Backoff delay
        time.sleep(2**i) 
    
    print("❌ Failed to notify evaluation server after multiple retries.")
    # Re-raise an exception so the FastAPI background task logs it as a failure
    raise Exception("Failed to notify evaluation server.")


# --- Combined Background Processor ---
def process_task(request_data: TaskRequest):
    """The main workflow that runs in the background for either round."""
    repo_name = get_repo_name(request_data.task)
    print(f"🚀 Starting Round {request_data.round} processing for task: {repo_name}")
    
    try:
        # --- NEW: Process Attachments for LLM Input ---
        # attachment_blocks is for vision (image), attachment_text_context is for text (CSV/JSON)
        attachment_blocks, attachment_text_context = get_attachment_context(request_data.attachments)
        
        # Store attachments that need to be committed to the repo (text/data files)
        attachments_to_commit = [
            att for att in request_data.attachments 
            if any(mime in att.url for mime in ['text/csv', 'application/json', 'text/markdown'])
        ]
        
        existing_code = None
        
        if request_data.round == 2:
            # --- ROUND 2: REVISE ---
            # 1. Get existing code to provide context to the LLM
            user = github_client.get_user()
            repo = user.get_repo(repo_name)
            
            # Fetch the contents of the existing index.html
            existing_contents = repo.get_contents("index.html")
            # The content is base64 encoded, so it must be decoded
            existing_code = base64.b64decode(existing_contents.content).decode('utf-8')

        # --- CODE GENERATION/REVISION STEP (COMMON TO BOTH ROUNDS) ---
        generated_files = generate_code_from_brief(
            request_data.brief, 
            request_data.checks,
            attachment_blocks,
            attachment_text_context,
            existing_code=existing_code
        )
        # Add attachments that need to be committed to the repo
        generated_files["attachments_to_commit"] = attachments_to_commit

        # --- DEPLOYMENT STEP ---
        if request_data.round == 1:
            # Create a new repo
            repo_details = create_and_deploy_repo(repo_name, generated_files)
        else: # request_data.round == 2
            # Update the existing repo
            repo_details = update_and_redeploy_repo(repo_name, generated_files)

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