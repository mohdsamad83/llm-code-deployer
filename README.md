# LLM Code Deployer API

[![Status](https://img.shields.io/badge/status-active-success.svg)](https://github.com/) 
[![Python Version](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![Framework](https://img.shields.io/badge/framework-FastAPI-green.svg)](https://fastapi.tiangolo.com/)

An automated application factory that receives a project brief, generates code using a Large Language Model (LLM), and deploys it as a live website on GitHub Pages.

---

## 🚀 Overview

This project is a fully automated system designed to handle the entire lifecycle of a small web application project: **Build, Deploy, and Revise**. It exposes a single API endpoint that accepts a JSON request detailing an application's requirements. The system then uses an LLM to generate the necessary HTML, CSS, and JavaScript, commits the files to a new GitHub repository, and provides a live URL via GitHub Pages. It also supports subsequent requests to revise and update the deployed application.

### Key Features

* **API-Driven Workflow**: All operations are triggered by a secure, secret-protected POST request.
* **LLM-Powered Code Generation**: Utilizes powerful models like GPT-4o to generate single-page web applications from a natural language brief.
* **Automated GitHub Integration**: Creates public repositories, commits generated files (`index.html`, `README.md`, `LICENSE`), and updates them for revision tasks.
* **Instant Deployment**: Automatically makes the generated application available via GitHub Pages.
* **Sophisticated Attachment Handling**: Processes data URI attachments, using images as visual context for the LLM and committing data files (e.g., `.csv`, `.json`) to the repo.
* **Asynchronous Processing**: Responds instantly with a `200 OK` while handling the entire build/deploy process in the background.
* **Resilient Notifications**: Uses an exponential backoff strategy to reliably notify an evaluation server upon task completion.

---

## 🛠️ Setup and Installation

Follow these steps to get the server running locally or ready for deployment.

### 1. Clone the Repository

```bash
git clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
cd your-repo-name
```

### 2. Install Dependencies

Ensure you have Python 3.9+ installed. Then, install the required packages using pip:

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

This project requires several secret keys and configuration variables. Create a file named `.env` in the root directory and add the following variables.

**File: `.env`**
```env
# A unique secret string to authenticate incoming requests from the evaluation server.
MY_SECRET="your-super-secret-string"

# A GitHub Personal Access Token (PAT) with `repo` scopes.
# Generate one at: [https://github.com/settings/tokens](https://github.com/settings/tokens)
GITHUB_TOKEN="ghp_YourGitHubToken..."

# Your API key from a service like AIPipe.org, which is compatible with OpenAI's SDK.
LLM_API_KEY="your-llm-api-key"

# Your GitHub username is required to construct the final GitHub Pages URL.
GITHUB_USERNAME="your-github-username"
```
*Note: You may need to install `python-dotenv` (`pip install python-dotenv`) for the `.env` file to be loaded automatically during local development.*

### 4. Run the Server Locally

You can run the application locally using `uvicorn`, a fast ASGI server.

```bash
uvicorn main:app --reload
```

The API will now be available at `http://127.0.0.1:8000`.

---

## ⚙️ API Usage

Interact with the application by sending a `POST` request to the `/api/deploy` endpoint.

* **Endpoint**: `/api/deploy`
* **Method**: `POST`
* **Headers**: `Content-Type: application/json`

### Request Body

The request body must be a JSON object with the following structure:

```json
{
  "email": "student@example.com",
  "secret": "your-super-secret-string",
  "task": "unique-task-id-123",
  "round": 1,
  "nonce": "ab12-...",
  "brief": "Create a page that displays the content of data.csv in a table.",
  "checks": [
    "Page has a <table> element",
    "Table contains at least 3 rows"
  ],
  "evaluation_url": "[https://example.com/notify](https://example.com/notify)",
  "attachments": [{ 
    "name": "data.csv", 
    "url": "data:text/csv;base64,..." 
  }]
}
```

### Responses

* **Success (200 OK)**: If the `secret` is valid, the server immediately responds with a success message, indicating that the task is being processed in the background.
    ```json
    {
      "status": "success",
      "message": "Round 1 task accepted and is being processed in the background."
    }
    ```
* **Error (403 Forbidden)**: The provided `secret` is invalid.
* **Error (422 Unprocessable Entity)**: The request body is missing fields or has incorrect data types.

---

## 📜 License

This project is licensed under the **MIT License**. See the `LICENSE` file for more details.