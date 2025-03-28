import os
import discord
import requests
import time
import json
import asyncio
import fitz  # ✅ PDF Processing (PyMuPDF)
import aiomysql  # ✅ Async MySQL Database
import openpyxl  # ✅ Excel (.XLSX) Processing
from docx import Document  # ✅ DOCX Processing
from openai import OpenAI
from urllib.parse import urlparse
from dotenv import load_dotenv

# Define the roles that are allowed to use the bot
# Start with Admin role and we'll add the bot's roles dynamically
ALLOWED_ROLES = {"Admin"}  # Admin is always allowed
BOT_ROLES = set()  # Will be populated with the bot's roles and refreshed periodically

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

# ✅ Establish Async MySQL Connection Pool
async def create_db_connection():
    try:
        pool = await aiomysql.create_pool(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            minsize=1,  # Minimum number of connections in the pool
            maxsize=5   # Maximum number of connections in the pool
        )
        print("✅ Connected to MySQL database (async)")
        return pool
    except Exception as e:
        print(f"❌ Async database connection failed: {e}")
        return None

# ✅ Ensure token_tracking table exists
async def ensure_token_tracking_table():
    global db_pool
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # First check if the table already exists
                await cursor.execute("SHOW TABLES LIKE 'token_tracking'")
                table_exists = await cursor.fetchone()
                
                if not table_exists:
                    # Only create the table if it doesn't exist
                    create_table_query = """
                    CREATE TABLE token_tracking (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        thread_id VARCHAR(255) NOT NULL,
                        model VARCHAR(255) DEFAULT NULL,
                        prompt_tokens INT NOT NULL,
                        completion_tokens INT NOT NULL,
                        total_tokens INT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                    await cursor.execute(create_table_query)
                    await conn.commit()
                    print("✅ Token tracking table created")
                else:
                    print("✅ Token tracking table already exists")
                
                # Check if user_lookup table exists
                await cursor.execute("SHOW TABLES LIKE 'user_lookup'")
                user_lookup_exists = await cursor.fetchone()
                
                if not user_lookup_exists:
                    # Create user_lookup table for mapping user_id to usernames
                    create_lookup_query = """
                    CREATE TABLE user_lookup (
                        user_id VARCHAR(255) PRIMARY KEY,
                        username VARCHAR(255) NOT NULL,
                        display_name VARCHAR(255),
                        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                    await cursor.execute(create_lookup_query)
                    await conn.commit()
                    print("✅ User lookup table created")
                else:
                    print("✅ User lookup table already exists")
    except Exception as e:
        print(f"❌ Failed to check/create tables: {e}")

# ✅ Function to log token usage
async def log_token_usage(user_id, thread_id, model, prompt_tokens, completion_tokens, total_tokens):
    global db_pool
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                query = """
                INSERT INTO token_tracking 
                (user_id, thread_id, model, prompt_tokens, completion_tokens, total_tokens) 
                VALUES (%s, %s, %s, %s, %s, %s)
                """
                await cursor.execute(query, (user_id, thread_id, model, prompt_tokens, completion_tokens, total_tokens))
                await conn.commit()
                print(f"✅ Logged token usage for user {user_id}: {total_tokens} tokens")
    except Exception as e:
        print(f"❌ Failed to log token usage: {e}")

# ✅ Global variable for MySQL connection pool
db_pool = None  

# ✅ Thread cache dictionary (in-memory storage for thread IDs)
thread_cache = {}

# ✅ Function to clear only stale cache entries (inactive for 24+ hours)
async def reset_thread_cache():
    global thread_cache, db_pool
    while True:
        await asyncio.sleep(3600)  # ✅ Check every 1 hour instead of every 24 hours

        try:
            async with db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    # ✅ Get user threads that haven't been used in the last 24 hours
                    query = """
                    SELECT user_id FROM user_threads 
                    WHERE last_used < NOW() - INTERVAL 24 HOUR
                    """
                    await cursor.execute(query)
                    results = await cursor.fetchall()

                    if results:
                        for user_id in results:
                            user_id = user_id[0]
                            if user_id in thread_cache:
                                del thread_cache[user_id]  # ✅ Remove only stale cache entries
                                print(f"🧹 ✅ Removed cached thread for inactive user {user_id}.")

                    else:
                        print("🔍 No stale cache entries found.")

        except Exception as e:
            print(f"⚠️ Error during thread cache cleanup: {e}")

# ✅ Track active threads to prevent duplicate processing
active_threads = set()

# Initialize OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Initialize Discord bot
intents = discord.Intents.default()
intents.messages = True
intents.dm_messages = True  # Enable DMs
intents.guilds = True  # Allows bot to see servers it's in
intents.members = True  # Enables fetching member roles
bot = discord.Client(intents=intents)

# Create directories for image & file storage
IMAGE_DIR = "discord-images"
FILE_DIR = "discord-files"
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(FILE_DIR, exist_ok=True)

# ✅ Async function to store user thread ID in MySQL and update last_used timestamp
async def save_thread(user_id, thread_id):
    global db_pool
    try:
        # ✅ Update cache
        thread_cache[user_id] = thread_id  

        # ✅ Store in MySQL and update timestamp
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                query = """
                INSERT INTO user_threads (user_id, thread_id, last_used) 
                VALUES (%s, %s, CURRENT_TIMESTAMP) AS new 
                ON DUPLICATE KEY UPDATE thread_id = new.thread_id, last_used = new.last_used
                """
                await cursor.execute(query, (user_id, thread_id))
                await conn.commit()
        print(f"✅ Stored thread ID for user {user_id}. (Cached & Async MySQL, Timestamp Updated)")
    except Exception as e:
        print(f"❌ Failed to store thread ID: {e}")

# ✅ Async function to get a user's thread ID from MySQL
async def get_thread_id(user_id):
    # ✅ Check if thread ID is in cache first
    if user_id in thread_cache:
        return thread_cache[user_id]

    global db_pool
    async with db_pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT thread_id FROM user_threads WHERE user_id = %s", (user_id,))
            result = await cursor.fetchone()

    if result:
        thread_cache[user_id] = result[0]  # ✅ Store in cache
        return result[0]

    return None  # No thread found

# ✅ Function to update username in lookup table
async def update_username_lookup(user_id, username, display_name=None):
    global db_pool
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                query = """
                INSERT INTO user_lookup (user_id, username, display_name, last_updated) 
                VALUES (%s, %s, %s, NOW()) AS new_user
                ON DUPLICATE KEY UPDATE 
                    username = new_user.username, 
                    display_name = new_user.display_name,
                    last_updated = NOW()
                """
                await cursor.execute(query, (user_id, username, display_name))
                await conn.commit()
                print(f"✅ Updated username mapping for {username} ({user_id})")
    except Exception as e:
        print(f"❌ Failed to update username lookup: {e}")

# ✅ Function to update bot roles from all servers
async def update_bot_roles():
    global BOT_ROLES
    # Clear existing roles to get a fresh list
    BOT_ROLES.clear()
    
    # Collect all roles the bot has across all servers
    for guild in bot.guilds:
        bot_member = guild.get_member(bot.user.id)
        if bot_member:
            # Add all the bot's roles (except @everyone which everyone has)
            for role in bot_member.roles:
                if role.name != "@everyone":
                    BOT_ROLES.add(role.name)
    
    print(f"✅ Bot roles refreshed: {', '.join(BOT_ROLES) if BOT_ROLES else 'No special roles'}")
    return BOT_ROLES

@bot.event
async def on_ready():
    global db_pool, BOT_ROLES
    db_pool = await create_db_connection()  # ✅ Create async DB connection
    
    # Initial role collection
    await update_bot_roles()
    
    if db_pool:
        print("✅ Async MySQL connection established.")
        # ✅ Ensure token tracking table exists
        await ensure_token_tracking_table()
        asyncio.create_task(reset_thread_cache())  # ✅ Start hourly cache cleanup for inactive users
    else:
        print("❌ Failed to connect to async MySQL.")

    print(f'✅ Logged in as {bot.user}')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return  # Ignore bot's own messages

    # Check for updated bot roles with each message
    await update_bot_roles()

    # Only process DMs
    if not isinstance(message.channel, discord.DMChannel):
        return  # Ignore messages outside of DMs

    # ✅ Fetch the user's roles from a mutual server
    user_roles = set()
    
    for guild in bot.guilds:  # Check each server the bot is in
        member = guild.get_member(message.author.id)  # Try to get the user
        if member:
            user_roles.update({role.name for role in member.roles})  # Add their roles
            break  # Stop after finding the first mutual server

    # ✅ Check if user has Admin role or any of the bot's roles
    if not (user_roles.intersection(ALLOWED_ROLES) or user_roles.intersection(BOT_ROLES)):
        await message.channel.send("🚫✨ **Access Denied**… for now! I'm still in *beta mode* 🧪 and only certain roles can chat with me right now. \n**Hang tight** — more access is coming soon! 💫🤖")
        return  # Stop further processing

    # ✅ User has permission, proceed with message processing
    asyncio.create_task(handle_user_message(message))  # ✅ Run in background

async def handle_user_message(message):
    if message.author == bot.user:
        return  # Ignore bot's own messages

    async with message.channel.typing():  # ✅ Bot shows "typing" indicator

        if isinstance(message.channel, discord.DMChannel):  # Only respond to DMs
            user_id = str(message.author.id)

            # ✅ Update username lookup table
            username = message.author.name
            display_name = getattr(message.author, 'display_name', username)
            await update_username_lookup(user_id, username, display_name)

            # ✅ Check if user has an existing thread, using cache first
            thread_id = await get_thread_id(user_id)

            if not thread_id:
                thread = await asyncio.to_thread(lambda: client.beta.threads.create())
                thread_id = thread.id
                print(f"✅ Created new thread for user {user_id}: {thread_id}")
            else:
                print(f"✅ Retrieved existing thread from cache/MySQL for user {user_id}: {thread_id}")

            # ✅ Always update the last_used timestamp in MySQL, even if thread exists
            await save_thread(user_id, thread_id)  # ✅ Ensures last_used updates every message

            try:
                content_data = []
                image_saved = False
                file_saved = False

                # ✅ Handle text messages
                if message.content:
                    print(f"📝 Received text: {message.content}")
                    content_data.append({"type": "text", "text": message.content})

                # ✅ Handle images and files
                if message.attachments:
                    for attachment in message.attachments:
                        file_url = attachment.url
                        filename = attachment.filename.lower()
                        parsed_url = urlparse(file_url)

                        # ✅ Check if the file type is supported
                        ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".txt", ".rtf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
                        file_extension = os.path.splitext(filename)[1]  # Extract the file extension
                        if file_extension not in ALLOWED_EXTENSIONS:
                            print(f"⚠️ Unsupported file type: {filename}")
                            await message.channel.send(
                                "⚠️ Unsupported file type detected. Please upload one of the supported formats:\n"
                                "📄 Documents: .pdf, .docx, .xlsx, .txt, .rtf\n"
                                "🖼️ Images: .png, .jpg, .jpeg, .gif, .webp"
                            )
                            return  # Stop processing this file

                        # ✅ Image Handling
                        if filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                            print(f"🔹 Detected image: {file_url}")
                            image_path = os.path.join(IMAGE_DIR, filename)

                            # ✅ Download the image
                            response = requests.get(file_url, stream=True)
                            if response.status_code == 200:
                                with open(image_path, "wb") as file:
                                    for chunk in response.iter_content(1024):
                                        file.write(chunk)
                                print(f"✅ Image successfully saved at: {image_path}")
                                image_saved = True
                            else:
                                print(f"❌ Image request failed, status code: {response.status_code}")
                                await message.channel.send("⚠️ Image download failed. Please try again.")
                                return

                            # ✅ Upload image to OpenAI
                            with open(image_path, "rb") as image_file:
                                try:
                                    file_response = await asyncio.to_thread(client.files.create, file=image_file, purpose="vision")
                                    file_id = file_response.id
                                    print(f"✅ Image uploaded to OpenAI. File ID: {file_id}")
                                    content_data.append({"type": "image_file", "image_file": {"file_id": file_id}})

                                    # ✅ Add image to content_data
                                    content_data.append({"type": "image_file", "image_file": {"file_id": file_id}})
                                except Exception as upload_error:
                                    print(f"❌ OpenAI upload failed: {upload_error}")
                                    await message.channel.send("⚠️ Image upload failed.")
                                    return
                            os.remove(image_path)

                        # ✅ File Handling (.DOCX, .PDF, .XLSX, .TXT, .RTF)
                        elif filename.endswith((".pdf", ".docx", ".xlsx", ".txt", ".rtf")):  # ✅ Added .RTF support
                            print(f"📄 Detected file: {file_url}")
                            file_path = os.path.join(FILE_DIR, filename)

                            # ✅ Download the file
                            response = requests.get(file_url, stream=True)
                            if response.status_code == 200:
                                with open(file_path, "wb") as file:
                                    for chunk in response.iter_content(1024):
                                        file.write(chunk)
                                print(f"✅ File successfully saved at: {file_path}")
                                file_saved = True
                            else:
                                print(f"❌ File request failed, status code: {response.status_code}")
                                await message.channel.send("⚠️ File download failed. Please try again.")
                                return

                            # ✅ Extract text from supported files
                            extracted_text = None
                            if filename.endswith(".pdf"):
                                with fitz.open(file_path) as pdf:
                                    extracted_text = "\n".join([page.get_text() for page in pdf])
                            elif filename.endswith(".docx"):
                                doc = Document(file_path)
                                extracted_text = "\n".join([para.text for para in doc.paragraphs])
                            elif filename.endswith(".xlsx"):
                                workbook = openpyxl.load_workbook(file_path)
                                extracted_text = "\n".join(
                                    [str(cell.value) for sheet in workbook.worksheets for row in sheet.iter_rows() for cell in row if cell.value]
                                )
                            elif filename.endswith(".txt"):
                                with open(file_path, "r", encoding="utf-8") as txt_file:
                                    extracted_text = txt_file.read()
                            elif filename.endswith(".rtf"):  # ✅ NEW RTF SUPPORT
                                from striprtf.striprtf import rtf_to_text
                                with open(file_path, "r", encoding="utf-8", errors="ignore") as rtf_file:
                                    extracted_text = rtf_to_text(rtf_file.read())  # ✅ Convert RTF to plain text
                                    extracted_text = extracted_text.encode("utf-8", "ignore").decode("utf-8")  # ✅ Fix encoding issues

                            # ✅ Add extracted text to OpenAI request
                            if extracted_text:
                                print(f"✅ Extracted {len(extracted_text)} characters from {filename}")
                                content_data.append({"type": "text", "text": extracted_text})
                            else:
                                print(f"⚠️ No readable content in {filename}")
                                await message.channel.send("⚠️ No readable content found in the file.")
                            os.remove(file_path)

    
                # ✅ Ensure OpenAI receives valid input
                if not content_data:
                    print("❌ No valid input for AI. Skipping processing.")
                    await message.channel.send("⚠️ Please send a message, an image, or a supported file.")
                    return

                # ✅ Check if thread is already running
                if thread_id in active_threads:
                    print(f"⚠️ Ignoring message in thread {thread_id} - already processing.")
                    return  # Simply ignore the message


                # ✅ Send message + image/file (if available) to OpenAI
                # ✅ Mark the thread as active
                active_threads.add(thread_id)

                try:
                    try:
                        # ✅ Attempt to send message to OpenAI using the existing thread ID
                        await asyncio.to_thread(client.beta.threads.messages.create, thread_id=thread_id, role="user", content=content_data)

                    except Exception as e:
                        error_message = str(e)

                        # ✅ If OpenAI returns a 404 (thread not found), create a new thread and retry
                        if "No thread found with id" in error_message:
                            print(f"⚠️ Thread {thread_id} not found. Creating a new one...")

                            # ✅ Create a new thread
                            thread = await asyncio.to_thread(lambda: client.beta.threads.create())
                            thread_id = thread.id

                            # ✅ Save the new thread ID in MySQL and cache
                            await save_thread(user_id, thread_id)

                            # ✅ Retry sending the message with the new thread
                            await asyncio.to_thread(client.beta.threads.messages.create, thread_id=thread_id, role="user", content=content_data)

                        else:
                            # ❌ Handle other errors
                            print(f"⚠️ Error sending message to OpenAI: {e}")
                            await message.channel.send("⚠️ Message could not be sent to AI. Please try again later.")
                except Exception as e:
                    print(f"⚠️ Error sending message to OpenAI: {e}")
                    await message.channel.send("⚠️ Message could not be sent to Cali. Please try again later.")
                    active_threads.discard(thread_id)  # ✅ Remove from active threads if failed
                    return  # Stop further processing

                # ✅ Show typing indicator while processing
                async with message.channel.typing():
                    print("⏳ Processing OpenAI request...")

                    # ✅ Run OpenAI processing first
                    run = await asyncio.to_thread(
                        client.beta.threads.runs.create_and_poll, 
                        thread_id=thread_id, 
                        assistant_id=ASSISTANT_ID,
                        truncation_strategy={  # ✅ Correctly placed here
                                            "type": "last_messages",
                                            "last_messages": 15
                                            }
                    )

                    # ✅ NEW: Get token usage after the run is complete
                    try:
                        # Get token usage directly from the run object
                        run_usage = None
                        model = "unknown"    # Default model
                        
                        # Get the model from the run if available
                        if hasattr(run, 'model'):
                            model = run.model
                        
                        # Get usage data directly from the run object
                        if hasattr(run, 'usage') and run.usage:
                            run_usage = run.usage
                            prompt_tokens = getattr(run_usage, 'prompt_tokens', 0)
                            completion_tokens = getattr(run_usage, 'completion_tokens', 0)
                            total_tokens = getattr(run_usage, 'total_tokens', 0)
                            
                            # Log token usage with model
                            await log_token_usage(user_id, thread_id, model, prompt_tokens, completion_tokens, total_tokens)
                            print(f"✅ Token usage recorded - Model: {model}, Prompt: {prompt_tokens}, Completion: {completion_tokens}, Total: {total_tokens}")
                        else:
                            print("⚠️ No token usage data available from the run")
                    
                    except Exception as e:
                        print(f"⚠️ Error recording token usage: {e}")

                    # ✅ Mark the thread as completed
                    active_threads.discard(thread_id)

                    # ✅ Now fetch the latest response
                    messages = await asyncio.to_thread(
                        client.beta.threads.messages.list,
                        thread_id=thread_id,
                        order="desc",
                        limit=1
                    )

                # ✅ Extract AI response
                if messages.data and messages.data[0].role == "assistant":
                    assistant_reply = messages.data[0].content[0].text.value
                else:
                    assistant_reply = "⚠️ No response from the assistant."

                # ✅ Send the AI response in a single message
                await send_long_message(message.channel, assistant_reply)


            except Exception as e:
                print(f"❌ Error: {e}")

# ✅ Function to send long messages in chunks
async def send_long_message(channel, text):
    """ Splits long messages into chunks of 2,000 characters and sends them sequentially. """
    max_length = 2000  # Discord's message limit

    for i in range(0, len(text), max_length):
        chunk = text[i:i + max_length]
        await channel.send(chunk)

# ✅ Async function to close the database connection pool on shutdown
async def close_db_connection():
    global db_pool
    if db_pool:
        db_pool.close()
        await db_pool.wait_closed()
        print("✅ Async database connection closed.")

import signal

async def shutdown():
    print("⏳ Initiating shutdown...")
    if db_pool:
        await close_db_connection()
    print("✅ Shutdown complete. Exiting process now...")
    os._exit(0)  # Force exit

def handle_shutdown():
    asyncio.create_task(shutdown())

signal.signal(signal.SIGTERM, lambda signum, frame: handle_shutdown())
signal.signal(signal.SIGINT, lambda signum, frame: handle_shutdown())

# Add these event handlers to update roles when joining/leaving servers
@bot.event
async def on_guild_join(guild):
    print(f"✅ Joined new server: {guild.name}")
    await update_bot_roles()  # Update roles when joining a new server

@bot.event
async def on_guild_remove(guild):
    print(f"⚠️ Left server: {guild.name}")
    await update_bot_roles()  # Update roles when leaving a server

@bot.event
async def on_member_update(before, after):
    # Check if it's the bot that was updated
    if before.id == bot.user.id:
        # Check if roles were changed
        if set(before.roles) != set(after.roles):
            print("✅ Bot roles changed, refreshing role list")
            await update_bot_roles()

# Run the bot
bot.run(DISCORD_TOKEN)