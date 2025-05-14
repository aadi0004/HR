import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
import speech_recognition as sr
import google.generativeai as genai
import pyttsx3
import time
import re
import mysql.connector
from mysql.connector import pooling
import sqlite3
from twilio.rest import Client
import asyncio
import logging
from fuzzywuzzy import process
from cachetools import TTLCache
from gtts import gTTS
import tempfile
import pygame
import uuid

# Setup logging to file only (no console output)
logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
    handlers=[logging.FileHandler('hr_assistant.log')]
)

# Load environment variables
logging.info("Loading environment variables...")
load_dotenv()
if not os.getenv("GEMINI_API_KEY"):
    logging.error("GEMINI_API_KEY not set in .env")
    raise ValueError("GEMINI_API_KEY not set in .env")
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# MySQL configuration with connection pooling
MYSQL_CONFIG = {
    'host': 'localhost',
    'database': 'regex_software',
    'user': os.getenv("MYSQL_USER", "regex_user"),
    'password': os.getenv("MYSQL_PASSWORD", "regex_password")
}
try:
    MYSQL_POOL = pooling.MySQLConnectionPool(pool_name="hr_pool", pool_size=5, **MYSQL_CONFIG)
    logging.info("MySQL pool initialized")
except Exception as e:
    logging.warning(f"MySQL pool initialization failed: {e}, using SQLite fallback")

# Twilio configuration
TWILIO_CONFIG = {
    'account_sid': os.getenv("TWILIO_ACCOUNT_SID"),
    'auth_token': os.getenv("TWILIO_AUTH_TOKEN"),
    'phone_number': os.getenv("TWILIO_PHONE_NUMBER")
}
if not all(TWILIO_CONFIG.values()):
    logging.warning("Twilio configuration incomplete, SMS functionality disabled")

# Response cache
RESPONSE_CACHE = {
    "schedule counseling": "Thanks, {name}! Do you have a course in mind, or would you like help choosing?",
    "reschedule counseling": "Thanks, {name}! When would you like to move your counseling session to?",
    "course details": "Thanks, {name}! Which course would you like to know about, like Python or Java?",
    "available courses": "Thanks, {name}! We offer Python, Java, Data Science, and Web Development. Want details on any?",
    "just schedule": "Thanks, {name}! Let's book your counseling session. When are you free, like May 15, 2025 at 11 AM?"
}

HR_PASSWORD = "regex123"

def clean_markdown(text):
    text = re.sub(r'\*{1,2}(.*?)\*{1,2}', r'\1', text)
    text = re.sub(r'_(.*?)_', r'\1', text)
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    return ' '.join(text.split()).strip()

def clean_name(name):
    prefixes = r'^(my name is|this is|i am)\s+'
    return re.sub(prefixes, '', name, flags=re.IGNORECASE).strip()

def detect_course(transcript_lower, prev_course=None):
    course_keywords = ["python", "java", "data science", "web development"]
    match, score = process.extractOne(transcript_lower, course_keywords)
    if score > 80:
        course_map = {
            "python": "Python Programming",
            "java": "Java Development",
            "data science": "Data Science",
            "web development": "Web Development"
        }
        return course_map[match]
    return prev_course

async def detect_intent(transcript_lower):
    intent_keywords = {
        "schedule": ["schedule", "book", "admission", "enroll", "join"],
        "reschedule": ["reschedule", "move", "change"],
        "course_details": ["details", "about", "tell me", "information"],
        "available_courses": ["available", "offer", "list", "courses"],
        "help_choose": ["suggest", "recommend", "which"],
        "hr_login": ["hr login", "admin", "hr access"],
        "logout": ["logout", "sign out"],
        "view_interactions": ["view interactions", "history", "past queries"],
        "status_report": ["status report", "report", "stats"],
        "update_course": ["update course", "change course", "modify course"]
    }
    for intent, keywords in intent_keywords.items():
        for keyword in keywords:
            if keyword in transcript_lower:
                return intent
    return "unknown"

async def connect_to_db():
    logging.info("Connecting to database...")
    for attempt in range(3):
        try:
            connection = MYSQL_POOL.get_connection()
            if connection.is_connected():
                logging.info("Connected to MySQL")
                return connection, 'mysql'
        except Exception as e:
            logging.error(f"DB connection attempt {attempt + 1} failed: {e}")
            time.sleep(0.5)
    logging.warning("Falling back to SQLite")
    sqlite_db = os.path.join(os.getcwd(), 'regex_software.db')
    connection = sqlite3.connect(sqlite_db)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection, 'sqlite'

async def create_tables():
    logging.info("Creating database tables...")
    connection, db_type = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
            employees_table = """
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id INTEGER PRIMARY KEY AUTO_INCREMENT,
                    name VARCHAR(255) NOT NULL,
                    phone_number VARCHAR(20),
                    sms_consent BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """ if db_type == 'mysql' else """
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    phone_number TEXT,
                    sms_consent INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            cursor.execute(employees_table)
            counseling_sessions_table = """
                CREATE TABLE IF NOT EXISTS counseling_sessions (
                    id INTEGER PRIMARY KEY AUTO_INCREMENT,
                    employee_id INTEGER NOT NULL,
                    session_date DATE NOT NULL,
                    session_time TIME NOT NULL,
                    mode ENUM('online', 'offline') NOT NULL DEFAULT 'offline',
                    course_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
                )
            """ if db_type == 'mysql' else """
                CREATE TABLE IF NOT EXISTS counseling_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL,
                    session_date TEXT NOT NULL,
                    session_time TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'offline',
                    course_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
                )
            """
            cursor.execute(counseling_sessions_table)
            interactions_table = """
                CREATE TABLE IF NOT EXISTS employee_interactions (
                    id INTEGER PRIMARY KEY AUTO_INCREMENT,
                    employee_id INTEGER NOT NULL,
                    employee_query TEXT NOT NULL,
                    ai_response TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
                )
            """ if db_type == 'mysql' else """
                CREATE TABLE IF NOT EXISTS employee_interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL,
                    employee_query TEXT NOT NULL,
                    ai_response TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
                )
            """
            cursor.execute(interactions_table)
            courses_table = """
                CREATE TABLE IF NOT EXISTS courses (
                    id INTEGER PRIMARY KEY AUTO_INCREMENT,
                    course_name VARCHAR(255) NOT NULL,
                    description TEXT,
                    duration VARCHAR(50),
                    fees DECIMAL(10,2) NOT NULL,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """ if db_type == 'mysql' else """
                CREATE TABLE IF NOT EXISTS courses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    course_name TEXT NOT NULL,
                    description TEXT,
                    duration TEXT,
                    fees REAL NOT NULL,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            cursor.execute(courses_table)
            hr_commands_table = """
                CREATE TABLE IF NOT EXISTS hr_commands (
                    id INTEGER PRIMARY KEY AUTO_INCREMENT,
                    command_text TEXT NOT NULL,
                    executed_by VARCHAR(255) NOT NULL,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """ if db_type == 'mysql' else """
                CREATE TABLE IF NOT EXISTS hr_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_text TEXT NOT NULL,
                    executed_by TEXT NOT NULL,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            cursor.execute(hr_commands_table)
            cursor.execute("SELECT COUNT(*) FROM courses")
            if cursor.fetchone()[0] == 0:
                sample_courses = [
                    (
                        "Python Programming",
                        "Learn Python from basics to advanced, covering data structures, OOP, and web development.",
                        "12 weeks",
                        15000.00,
                        "Basics, variables, loops, functions, OOP, data structures, file handling, web development with Flask, database integration."
                    ),
                    (
                        "Java Development",
                        "Master Java for enterprise applications, including Spring and Hibernate frameworks.",
                        "10 weeks",
                        18000.00,
                        "Core Java, OOP, collections, multithreading, Spring, Hibernate, REST APIs, database connectivity."
                    ),
                    (
                        "Data Science",
                        "Explore data analysis, machine learning, and visualization with Python and R.",
                        "14 weeks",
                        22000.00,
                        "Statistics, Python, R, pandas, NumPy, machine learning, deep learning, data visualization, big data basics."
                    ),
                    (
                        "Web Development",
                        "Build modern websites with HTML, CSS, JavaScript, and React.",
                        "8 weeks",
                        13000.00,
                        "HTML, CSS, JavaScript, DOM, React, Redux, API integration, responsive design."
                    )
                ]
                cursor.executemany(
                    "INSERT INTO courses (course_name, description, duration, fees, content) VALUES (?, ?, ?, ?, ?)" if db_type == 'sqlite' else
                    "INSERT INTO courses (course_name, description, duration, fees, content) VALUES (%s, %s, %s, %s, %s)",
                    sample_courses
                )
            connection.commit()
            logging.info(f"Database tables ensured ({db_type})")
        except Exception as e:
            logging.error(f"Error creating tables: {e}")
            if db_type == 'mysql':
                connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()

async def save_interaction(employee_id, query, response):
    connection, db_type = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
            cursor.execute(
                "INSERT INTO employee_interactions (employee_id, employee_query, ai_response) VALUES (?, ?, ?)" if db_type == 'sqlite' else
                "INSERT INTO employee_interactions (employee_id, employee_query, ai_response) VALUES (%s, %s, %s)",
                (employee_id, query, response)
            )
            connection.commit()
            logging.info(f"Saved interaction for employee ID {employee_id}")
        except Exception as e:
            logging.error(f"Error saving interaction: {e}")
            if db_type == 'mysql':
                connection.rollback()
        finally:
            cursor.close()
            connection.close()

async def save_hr_command(command_text, executed_by):
    connection, db_type = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
            cursor.execute(
                "INSERT INTO hr_commands (command_text, executed_by) VALUES (?, ?)" if db_type == 'sqlite' else
                "INSERT INTO hr_commands (command_text, executed_by) VALUES (%s, %s)",
                (command_text, executed_by)
            )
            connection.commit()
            logging.info(f"Saved HR command: {command_text} by {executed_by}")
        except Exception as e:
            logging.error(f"Error saving HR command: {e}")
            if db_type == 'mysql':
                connection.rollback()
        finally:
            cursor.close()
            connection.close()

async def get_or_create_employee(name, phone_number=None, sms_consent=True):
    connection, db_type = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
            cursor.execute(
                "SELECT employee_id, name, phone_number, sms_consent FROM employees WHERE name = ?" if db_type == 'sqlite' else
                "SELECT employee_id, name, phone_number, sms_consent FROM employees WHERE name = %s",
                (name,)
            )
            result = cursor.fetchone()
            if result:
                employee_id, stored_name, stored_phone, stored_consent = result
                if phone_number and stored_phone != phone_number:
                    cursor.execute(
                        "UPDATE employees SET phone_number = ?, sms_consent = ? WHERE employee_id = ?" if db_type == 'sqlite' else
                        "UPDATE employees SET phone_number = %s, sms_consent = %s WHERE employee_id = %s",
                        (phone_number, sms_consent, employee_id)
                    )
                    connection.commit()
                return employee_id, stored_name, stored_phone or phone_number, stored_consent
            cursor.execute(
                "INSERT INTO employees (name, phone_number, sms_consent) VALUES (?, ?, ?)" if db_type == 'sqlite' else
                "INSERT INTO employees (name, phone_number, sms_consent) VALUES (%s, %s, %s)",
                (name, phone_number, sms_consent)
            )
            connection.commit()
            cursor.execute("SELECT LAST_INSERT_ID()" if db_type == 'mysql' else "SELECT last_insert_rowid()")
            employee_id = cursor.fetchone()[0]
            return employee_id, name, phone_number, sms_consent
        except Exception as e:
            logging.error(f"Error managing employee: {e}")
            if db_type == 'mysql':
                connection.rollback()
            return None, name, None, True
        finally:
            cursor.close()
            connection.close()
    return None, name, None, True

async def fetch_session(employee_id):
    connection, db_type = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
            cursor.execute(
                "SELECT id, session_date, session_time, mode, course_id FROM counseling_sessions WHERE employee_id = ? ORDER BY created_at DESC LIMIT 1" if db_type == 'sqlite' else
                "SELECT id, session_date, session_time, mode, course_id FROM counseling_sessions WHERE employee_id = %s ORDER BY created_at DESC LIMIT 1",
                (employee_id,)
            )
            result = cursor.fetchone()
            if result:
                return {'id': result[0], 'date': result[1], 'time': result[2], 'mode': result[3], 'course_id': result[4]}
            return None
        except Exception as e:
            logging.error(f"Error fetching session: {e}")
            return None
        finally:
            cursor.close()
            connection.close()
    return None

async def fetch_course_details(course_name):
    course_cache = TTLCache(maxsize=100, ttl=300)
    if course_name in course_cache:
        return course_cache[course_name]['details']
    connection, db_type = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
            cursor.execute(
                "SELECT id, course_name, description, duration, fees, content FROM courses WHERE course_name LIKE ?" if db_type == 'sqlite' else
                "SELECT id, course_name, description, duration, fees, content FROM courses WHERE course_name LIKE %s",
                (f"%{course_name}%",)
            )
            result = cursor.fetchone()
            if result:
                details = {
                    'id': result[0],
                    'name': result[1],
                    'description': result[2],
                    'duration': result[3],
                    'fees': result[4],
                    'content': result[5]
                }
                course_cache[course_name] = {'details': details, 'timestamp': datetime.now()}
                return details
            return None
        except Exception as e:
            logging.error(f"Error fetching course: {e}")
            return None
        finally:
            cursor.close()
            connection.close()
    return None

async def fetch_employee_interactions(employee_name):
    connection, db_type = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
            cursor.execute(
                """
                SELECT ei.employee_query, ei.ai_response, ei.created_at
                FROM employee_interactions ei
                JOIN employees e ON ei.employee_id = e.employee_id
                WHERE e.name LIKE ?
                ORDER BY ei.created_at DESC
                LIMIT 5
                """ if db_type == 'sqlite' else
                """
                SELECT ei.employee_query, ei.ai_response, ei.created_at
                FROM employee_interactions ei
                JOIN employees e ON ei.employee_id = e.employee_id
                WHERE e.name LIKE %s
                ORDER BY ei.created_at DESC
                LIMIT 5
                """,
                (f"%{employee_name}%",)
            )
            results = cursor.fetchall()
            return [f"Query: {row[0]}, Response: {row[1]}, Time: {row[2]}" for row in results]
        except Exception as e:
            logging.error(f"Error fetching interactions: {e}")
            return []
        finally:
            cursor.close()
            connection.close()
    return []

async def generate_status_report():
    connection, db_type = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM counseling_sessions")
            sessions = cursor.fetchone()[0]
            return f"Total counseling sessions: {sessions}"
        except Exception as e:
            logging.error(f"Error generating report: {e}")
            return "Error generating report"
        finally:
            cursor.close()
            connection.close()
    return "Database connection failed"

async def update_course_details(course_name, field, value, executed_by):
    connection, db_type = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
            valid_fields = {'price': 'fees', 'description': 'description', 'content': 'content'}
            if field.lower() not in valid_fields:
                return "Invalid field. Use price, description, or content"
            db_field = valid_fields[field.lower()]
            if db_field == 'fees':
                value = float(value)
                if value <= 12000:
                    return "Course price must be above 12,000 INR"
            cursor.execute(
                f"UPDATE courses SET {db_field} = ? WHERE course_name LIKE ?" if db_type == 'sqlite' else
                f"UPDATE courses SET {db_field} = %s WHERE course_name LIKE %s",
                (value, f"%{course_name}%")
            )
            if cursor.rowcount > 0:
                connection.commit()
                await save_hr_command(f"Updated {field} of {course_name} to {value}", executed_by)
                return f"Updated {field} for {course_name} successfully"
            return "Course not found"
        except Exception as e:
            logging.error(f"Error updating course: {e}")
            if db_type == 'mysql':
                connection.rollback()
            return "Error updating course"
        finally:
            cursor.close()
            connection.close()
    return "Database connection failed"

async def check_availability(session_date, session_time):
    connection, db_type = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM counseling_sessions WHERE session_date = ? AND session_time = ?" if db_type == 'sqlite' else
                "SELECT COUNT(*) FROM counseling_sessions WHERE session_date = %s AND session_time = %s",
                (session_date, session_time)
            )
            return cursor.fetchone()[0] == 0
        except Exception as e:
            logging.error(f"Error checking availability: {e}")
            return False
        finally:
            cursor.close()
            connection.close()
    return False

async def send_sms(employee_name, employee_phone, message_body):
    if not employee_phone or not TWILIO_CONFIG['phone_number']:
        logging.error("Missing phone number or Twilio config")
        return False
    to_number = f"+91{employee_phone}" if not employee_phone.startswith('+') else employee_phone
    try:
        twilio_client = Client(TWILIO_CONFIG['account_sid'], TWILIO_CONFIG['auth_token'])
        message = twilio_client.messages.create(
            body=f"Hi {employee_name}, {message_body} Contact HR at (555) 987-6543. -Regex Software HR",
            from_=TWILIO_CONFIG['phone_number'],
            to=to_number
        )
        logging.info(f"SMS sent to {employee_phone} (SID: {message.sid})")
        return True
    except Exception as e:
        logging.error(f"Failed to send SMS: {e}")
        return False

async def send_session_message(employee_name, employee_phone, sms_consent, session_date, session_time, mode, course_name=None, is_reschedule=False):
    if employee_phone and sms_consent and session_date and session_time:
        action = "rescheduled" if is_reschedule else "scheduled"
        location = "at our office" if mode == "offline" else "online"
        course_text = f" for {course_name}" if course_name else ""
        body = f"your {mode} counseling session{course_text} {location} is {action} for {session_date} at {session_time}."
        success = await send_sms(employee_name, employee_phone, body)
        if not success:
            return f"Your {mode} session{course_text} {location} is {action} for {session_date} at {session_time}, but SMS failed."
        return f"Your {mode} session{course_text} {location} is {action} for {session_date} at {session_time}."
    course_text = f" for {course_name}" if course_name else ""
    action = "rescheduled" if is_reschedule else "scheduled"
    location = "at our office" if mode == "offline" else "online"
    return f"Your {mode} session{course_text} {location} is {action} for {session_date} at {session_time}."

async def autoschedule_offline_session(employee_id, employee_name, employee_phone, sms_consent, course_name):
    start_date = datetime(2025, 5, 14)
    time_slots = ["10:00", "14:00", "16:00"]
    max_attempts = 30
    connection, db_type = await connect_to_db()
    if not connection:
        return "Database connection failed"
    try:
        cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
        cursor.execute(
            "SELECT id FROM courses WHERE course_name LIKE ?" if db_type == 'sqlite' else
            "SELECT id FROM courses WHERE course_name LIKE %s",
            (f"%{course_name}%",)
        )
        course_id = cursor.fetchone()[0] if cursor.rowcount else None
        for attempt in range(max_attempts):
            session_date = (start_date + timedelta(days=attempt)).strftime("%Y-%m-%d")
            for slot_time in time_slots:
                if await check_availability(session_date, slot_time):
                    cursor.execute(
                        "INSERT INTO counseling_sessions (employee_id, session_date, session_time, mode, course_id) VALUES (?, ?, ?, ?, ?)" if db_type == 'sqlite' else
                        "INSERT INTO counseling_sessions (employee_id, session_date, session_time, mode, course_id) VALUES (%s, %s, %s, %s, %s)",
                        (employee_id, session_date, slot_time, 'offline', course_id)
                    )
                    if cursor.rowcount > 0:
                        connection.commit()
                        return await send_session_message(employee_name, employee_phone, sms_consent, session_date, slot_time, 'offline', course_name)
                    connection.rollback()
                    return "Failed to autoschedule session"
        return "No available slots for the next 30 days"
    except Exception as e:
        logging.error(f"Error autoscheduling session: {e}")
        if db_type == 'mysql':
            connection.rollback()
        return "Error autoscheduling session"
    finally:
        cursor.close()
        connection.close()

async def conduct_online_counseling(employee_name, course_name, chat_session):
    course_details = await fetch_course_details(course_name)
    if not course_details:
        return f"Sorry, couldn't find {course_name}. I'll schedule an offline session."
    counseling_prompt = f"""
    You are Emma, a professional HR assistant at Regex Software. Conduct a concise (max 150 words) online counseling session for {employee_name} about {course_name}. Use only:
    - Description: {course_details['description']}
    - Duration: {course_details['duration']}
    - Fees: INR {course_details['fees']}
    - Content: {course_details['content']}
    Do not mention other courses. Highlight benefits (skills, career growth). Ask about goals, align with course. End with: 'What are your thoughts, {employee_name}? Want to proceed or schedule a follow-up?'
    """
    try:
        response = await asyncio.to_thread(chat_session.send_message, counseling_prompt)
        counseling_response = response.text.strip() or f"Hi {employee_name}, the {course_name} course ({course_details['description']}) lasts {course_details['duration']} and costs INR {course_details['fees']}. It covers {course_details['content']}. It’s great for career growth! What are your goals? Want to proceed or schedule a follow-up?"
        return clean_markdown(counseling_response)
    except Exception as e:
        logging.error(f"Error in online counseling: {e}")
        return f"Sorry, couldn't conduct online session for {course_name}. I'll schedule an offline session."

async def save_session(employee_id, employee_name, employee_phone, sms_consent, response, mode, course_name):
    session_pattern = r"(?:scheduled|booked|set|rescheduled)\s*(?:for|on|at)?\s*(\d{4}-\d{2}-\d{2})\s*(?:at)?\s*(\d{2}:\d{2}(?:\s*(?:AM|PM))?)"
    match = re.search(session_pattern, response, re.IGNORECASE)
    if match:
        session_date, session_time = match.group(1), match.group(2)
        if int(session_date.split('-')[0]) < 2025:
            session_date = f"2025{session_date[4:]}"
        if "AM" in session_time.upper() or "PM" in session_time.upper():
            try:
                time_obj = datetime.strptime(session_time, "%I:%M %p")
                session_time = time_obj.strftime("%H:%M")
            except ValueError:
                return "Invalid time format. Say again, like 11:00 AM"
        if await check_availability(session_date, session_time):
            connection, db_type = await connect_to_db()
            if connection:
                try:
                    cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
                    cursor.execute(
                        "SELECT id FROM courses WHERE course_name LIKE ?" if db_type == 'sqlite' else
                        "SELECT id FROM courses WHERE course_name LIKE %s",
                        (f"%{course_name}%",)
                    )
                    course_id = cursor.fetchone()[0] if cursor.rowcount else None
                    cursor.execute(
                        "INSERT INTO counseling_sessions (employee_id, session_date, session_time, mode, course_id) VALUES (?, ?, ?, ?, ?)" if db_type == 'sqlite' else
                        "INSERT INTO counseling_sessions (employee_id, session_date, session_time, mode, course_id) VALUES (%s, %s, %s, %s, %s)",
                        (employee_id, session_date, session_time, mode, course_id)
                    )
                    if cursor.rowcount > 0:
                        connection.commit()
                        return await send_session_message(employee_name, employee_phone, sms_consent, session_date, session_time, mode, course_name)
                    connection.rollback()
                    return "Failed to book session"
                except Exception as e:
                    logging.error(f"Error saving session: {e}")
                    if db_type == 'mysql':
                        connection.rollback()
                    return "Error booking session"
                finally:
                    cursor.close()
                    connection.close()
        return "Slot taken. Choose another, like May 16, 2025 at 11 AM"
    return "No date/time detected. Say again, like May 15, 2025 at 11 AM"

async def reschedule_session(employee_id, employee_name, employee_phone, sms_consent, response, mode, course_name):
    session_pattern = r"(?:re)?scheduled\s*(?:for|to|on|at)?\s*(\d{4}-\d{2}-\d{2})\s*(?:at)?\s*(\d{2}:\d{2}(?:\s*(?:AM|PM))?)"
    match = re.search(session_pattern, response, re.IGNORECASE)
    if match:
        session_date, session_time = match.group(1), match.group(2)
        if int(session_date.split('-')[0]) < 2025:
            session_date = f"2025{session_date[4:]}"
        if "AM" in session_time.upper() or "PM" in session_time.upper():
            try:
                time_obj = datetime.strptime(session_time, "%I:%M %p")
                session_time = time_obj.strftime("%H:%M")
            except ValueError:
                return "Invalid time format. Say again, like 11:00 AM"
        if await check_availability(session_date, session_time):
            existing_session = await fetch_session(employee_id)
            if existing_session:
                connection, db_type = await connect_to_db()
                if connection:
                    try:
                        cursor = connection.cursor(buffered=True) if db_type == 'mysql' else connection.cursor()
                        cursor.execute(
                            "SELECT id FROM courses WHERE course_name LIKE ?" if db_type == 'sqlite' else
                            "SELECT id FROM courses WHERE course_name LIKE %s",
                            (f"%{course_name}%",)
                        )
                        course_id = cursor.fetchone()[0] if cursor.rowcount else None
                        cursor.execute(
                            "UPDATE counseling_sessions SET session_date = ?, session_time = ?, mode = ?, course_id = ?, created_at = CURRENT_TIMESTAMP WHERE id = ? AND employee_id = ?" if db_type == 'sqlite' else
                            "UPDATE counseling_sessions SET session_date = %s, session_time = %s, mode = %s, course_id = %s, created_at = CURRENT_TIMESTAMP WHERE id = %s AND employee_id = %s",
                            (session_date, session_time, mode, course_id, existing_session['id'], employee_id)
                        )
                        if cursor.rowcount > 0:
                            connection.commit()
                            return await send_session_message(employee_name, employee_phone, sms_consent, session_date, session_time, mode, course_name, is_reschedule=True)
                        connection.rollback()
                        return "Failed to reschedule session"
                    except Exception as e:
                        logging.error(f"Error rescheduling session: {e}")
                        if db_type == 'mysql':
                            connection.rollback()
                        return "Error rescheduling session"
                    finally:
                        cursor.close()
                        connection.close()
            return await save_session(employee_id, employee_name, employee_phone, sms_consent, response, mode, course_name)
        return "Slot taken. Choose another, like May 16, 2025 at 11 AM"
    return "No date/time detected. Say again, like May 15, 2025 at 11 AM"

class HR_Assistant:
    def __init__(self):
        logging.info("Initializing HR_Assistant...")
        self.first_run = not os.path.exists('hr_assistant.log')
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()
        self.recognizer.energy_threshold = 3000
        self.recognizer.dynamic_energy_threshold = True
        self.language = 'en'
        try:
            self.model = genai.GenerativeModel("gemini-1.5-flash")
            self.chat = self.model.start_chat(history=[
                {"role": "user", "parts": [
                    """You are Emma, a friendly HR assistant at Regex Software. Assist with scheduling counseling, course details, and HR tasks. 
                    Understand natural language queries (e.g., 'take admission' means schedule counseling). 
                    For unrelated topics (e.g., sports), say: 'I can only assist with courses like Python, Java, Data Science, or Web Development.' 
                    For counseling, ask 'Do you have a course in mind, or need help choosing?' only if no course is specified. 
                    If a course is mentioned, confirm and ask: 'Online or offline at our office?' 
                    For online, conduct immediate counseling with Gemini, using only selected course details. If it fails, autoschedule offline. 
                    For offline, schedule at office. Use prior course context if available. 
                    If help is needed, list database courses (name, description, fees, duration, content), then ask to schedule. 
                    Confirm offline with 'Okay [name], your offline session for [course] is scheduled for YYYY-MM-DD at HH:MM.' 
                    For HR (after 'HR login' and password), handle view interactions, update courses, or reports. 
                    Use only employee name, not phone number. Dates in 2025+. End with 'What else can I help you with?'"""
                ]},
                {"role": "model", "parts": ["Hello, I'm Emma, your HR assistant at Regex Software. How can I assist you today?"]}])
        except Exception as e:
            logging.error(f"Error initializing Gemini model: {e}")
            raise
        self.tts_engine = pyttsx3.init()
        self.tts_engine.setProperty('rate', 170)
        self.tts_engine.setProperty('volume', 0.9)
        voices = self.tts_engine.getProperty('voices')
        for voice in voices:
            if "Zira" in voice.name:
                self.tts_engine.setProperty('voice', voice.id)
                break
        try:
            pygame.mixer.init()
        except Exception as e:
            logging.error(f"Error initializing pygame mixer: {e}")
            raise
        self.employee_name = "Unknown"
        self.employee_id = None
        self.employee_phone = None
        self.sms_consent = True
        self.last_intent = None
        self.counseling_context = None
        self.selected_course = None
        self.counseling_mode = None
        self.is_hr_authenticated = False
        self.hr_user = "HR_Admin"
        self.audio_cache = TTLCache(maxsize=50, ttl=3600)
        self.response_cache = RESPONSE_CACHE
        self.audio_failure_count = 0
        self.error_count = 0
        self.last_interaction = time.time()
        self.timeout_seconds = 300
        self.phrase_cache = {"online": "online", "offline": "offline"}
        logging.info("HR_Assistant initialized successfully")

    def cleanup(self):
        try:
            self.tts_engine.stop()
            pygame.mixer.quit()
        except Exception as e:
            logging.error(f"Error during cleanup: {e}")

    async def generate_audio(self, text, raw_response):
        cache_key = f"{text}_en"
        if cache_key in self.audio_cache:
            try:
                pygame.mixer.music.load(self.audio_cache[cache_key])
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    await asyncio.sleep(0.1)
                return
            except Exception as e:
                logging.error(f"Error playing cached audio: {e}")
        try:
            tts = gTTS(text=text, lang='en', slow=False)
            temp_filename = os.path.join(tempfile.gettempdir(), f"hr_assistant_{uuid.uuid4().hex}.mp3")
            logging.info(f"Saving audio to {temp_filename}")
            tts.save(temp_filename)
            await asyncio.sleep(0.5)
            if not os.path.exists(temp_filename):
                logging.error(f"Audio file {temp_filename} not found after saving")
                raise FileNotFoundError(f"Audio file {temp_filename} not found")
            logging.info(f"Audio file {temp_filename} saved successfully")
            self.audio_cache[cache_key] = temp_filename
            pygame.mixer.music.load(temp_filename)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                await asyncio.sleep(0.1)
            # Retry file deletion to handle WinError 32
            for attempt in range(3):
                try:
                    os.remove(temp_filename)
                    logging.info(f"Removed temporary audio file {temp_filename}")
                    break
                except PermissionError as e:
                    logging.warning(f"Attempt {attempt + 1} to remove {temp_filename} failed: {e}")
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logging.warning(f"Error removing temporary file {temp_filename}: {e}")
                    break
        except Exception as e:
            logging.error(f"Audio generation error: {e}")
            self.tts_engine.say("Sorry, audio failed. Please try again.")
            self.tts_engine.runAndWait()

    async def get_employee_details(self):
        logging.info("Getting employee details...")
        max_attempts = 3
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=2.0)
            if self.first_run:
                tutorial_msg = (
                    "Welcome to Regex HR Assistant! First, provide your name and phone number. "
                    "You can ask about courses, schedule counseling, or get course details. "
                    "Try it now: say your name or 'skip'."
                )
                await self.generate_audio(tutorial_msg, tutorial_msg)
                self.first_run = False
            name_prompt = "Please say your name or say 'skip'."
            for attempt in range(max_attempts):
                await self.generate_audio(name_prompt, name_prompt)
                try:
                    audio = self.recognizer.listen(source, timeout=10, phrase_time_limit=10)
                    name = self.recognizer.recognize_google(audio, language='en-US').strip().lower()
                    logging.info(f"Raw name input: {name}")
                    if "skip" in name:
                        self.employee_name = "Guest"
                        break
                    if name:
                        self.employee_name = clean_name(name)
                        break
                    if attempt == max_attempts - 1:
                        self.employee_name = "Guest"
                        await self.generate_audio("No name detected, using 'Guest'.", "No name detected, using 'Guest'.")
                except sr.WaitTimeoutError:
                    logging.error("Name capture timeout: No speech detected")
                    if attempt < max_attempts - 1:
                        await self.generate_audio("No speech detected. Say your name or 'skip'.", "No speech detected. Say your name or 'skip'.")
                    else:
                        self.employee_name = "Guest"
                        await self.generate_audio("No name detected, using 'Guest'.", "No name detected, using 'Guest'.")
                except Exception as e:
                    logging.error(f"Name capture error: {e}")
                    if attempt < max_attempts - 1:
                        await self.generate_audio("Didn't catch your name. Say your name or 'skip'.", "Didn't catch your name. Say your name or 'skip'.")
            self.employee_id, self.employee_name, self.employee_phone, self.sms_consent = await get_or_create_employee(
                self.employee_name, self.employee_phone, self.sms_consent)
            if not self.employee_phone:
                phone_prompt = "Say your 10-digit phone number or say 'skip'."
                for attempt in range(max_attempts):
                    await self.generate_audio(phone_prompt, phone_prompt)
                    try:
                        audio = self.recognizer.listen(source, timeout=10, phrase_time_limit=10)
                        phone = self.recognizer.recognize_google(audio, language='en-US').strip().lower()
                        logging.info(f"Raw phone input: {phone}")
                        if "skip" in phone:
                            self.sms_consent = False
                            break
                        phone = re.sub(r'\D', '', phone)
                        if len(phone) >= 10:
                            self.employee_phone = phone[:10]
                            self.employee_id, _, self.employee_phone, self.sms_consent = await get_or_create_employee(
                                self.employee_name, self.employee_phone, self.sms_consent)
                            break
                        if attempt == max_attempts - 1:
                            self.sms_consent = False
                            await self.generate_audio("No valid phone number, SMS disabled.", "No valid phone number, SMS disabled.")
                    except sr.WaitTimeoutError:
                        logging.error("Phone capture timeout: No speech detected")
                        if attempt < max_attempts - 1:
                            await self.generate_audio("No speech detected. Say your 10-digit phone number or 'skip'.", 
                                                     "No speech detected. Say your 10-digit phone number or 'skip'.")
                        else:
                            self.sms_consent = False
                            await self.generate_audio("No valid phone number, SMS disabled.", "No valid phone number, SMS disabled.")
                    except Exception as e:
                        logging.error(f"Phone capture error: {e}")
                        if attempt < max_attempts - 1:
                            await self.generate_audio("Didn't catch your phone number. Say your 10-digit phone number or 'skip'.", 
                                                     "Didn't catch your phone number. Say your 10-digit phone number or 'skip'.")
            welcome_msg = f"Hi, {self.employee_name}! How can I assist you today?"
            await self.generate_audio(welcome_msg, welcome_msg)

    async def start_transcription(self):
        logging.info("Starting transcription...")
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=2.0)
            while True:
                try:
                    logging.info("Listening for audio input...")
                    audio = self.recognizer.listen(source, timeout=10, phrase_time_limit=10)
                    self.last_interaction = time.time()
                    transcript = self.recognizer.recognize_google(audio, language='en-US').strip()
                    logging.info(f"Transcript: {transcript}")
                    print(f"\nEmployee: {transcript}\n")
                    self.audio_failure_count = 0
                    response = await self.generate_ai_response(transcript, transcript)
                    print(f"Emma: {response}\n")
                except sr.WaitTimeoutError:
                    logging.info("Transcription timeout: No speech detected")
                    if time.time() - self.last_interaction > self.timeout_seconds:
                        response = "Session timed out. Starting over."
                        print(f"Emma: {response}\n")
                        await self.generate_audio(response, response)
                        self.__init__()
                        await self.get_employee_details()
                    else:
                        self.audio_failure_count += 1
                        if self.audio_failure_count >= 3:
                            response = "Try asking about courses, scheduling counseling, or course details."
                            print(f"Emma: {response}\n")
                            await self.generate_audio(response, response)
                            self.audio_failure_count = 0
                except Exception as e:
                    logging.error(f"Transcription error: {e}")
                    self.audio_failure_count += 1
                    if self.audio_failure_count >= 3:
                        response = "Trouble hearing you. Try again or type your query."
                        print(f"Emma: {response}\n")
                        await self.generate_audio(response, response)
                        user_input = input("Type your query (or press Enter to retry speech): ")
                        if user_input.strip():
                            response = await self.generate_ai_response(user_input, user_input)
                            print(f"Emma: {response}\n")
                            self.audio_failure_count = 0
                    self.error_count += 1
                    if self.error_count >= 5:
                        logging.error("Too many errors, restarting assistant")
                        self.cleanup()
                        self.__init__()
                        await self.get_employee_details()
                        self.error_count = 0

    async def generate_ai_response(self, transcript_en, transcript_raw):
        logging.info(f"Processing transcript: {transcript_en}")
        try:
            if not self.employee_id or self.employee_name == "Unknown":
                await self.get_employee_details()
                return "Hi, I'm Emma, your HR assistant at Regex Software. How can I assist you today?"

            transcript_lower = transcript_en.lower().strip()
            if transcript_lower in self.phrase_cache:
                transcript_en = self.phrase_cache[transcript_lower]

            if "hr login" in transcript_lower:
                if HR_PASSWORD in transcript_lower:
                    self.is_hr_authenticated = True
                    self.hr_user = self.employee_name
                    ai_response = "HR login successful. View interactions, update courses, or generate reports?"
                    await self.generate_audio(ai_response, ai_response)
                    await save_interaction(self.employee_id, transcript_raw, ai_response)
                    return ai_response
                ai_response = "Please say the correct HR password."
                await self.generate_audio(ai_response, ai_response)
                await save_interaction(self.employee_id, transcript_raw, ai_response)
                return ai_response

            if self.is_hr_authenticated:
                if "view interactions" in transcript_lower:
                    name_match = re.search(r'for\s+(.+?)(?:\s+code|$)', transcript_lower)
                    if name_match:
                        interactions = await fetch_employee_interactions(employee_name=name_match.group(1).strip())
                        identifier = f"name {name_match.group(1).strip()}"
                    else:
                        interactions = await fetch_employee_interactions(employee_name=self.employee_name)
                        identifier = f"name {self.employee_name}"
                    ai_response = f"Interactions for {identifier}:\n" + "\n".join(interactions) if interactions else f"No interactions for {identifier}."
                    ai_response += "\nWhat else can I help you with?"
                    await self.generate_audio(ai_response, ai_response)
                    await save_interaction(self.employee_id, transcript_raw, ai_response)
                    await save_hr_command(f"Viewed interactions for {identifier}", self.hr_user)
                    return ai_response
                elif "update course" in transcript_lower:
                    match = re.search(r"update course (\w+) (price|description|content) to (.+)", transcript_lower, re.IGNORECASE)
                    if match:
                        course_name, field, value = match.groups()
                        ai_response = await update_course_details(course_name, field, value, self.hr_user)
                        ai_response += " What else can I help you with?"
                        await self.generate_audio(ai_response, ai_response)
                        await save_interaction(self.employee_id, transcript_raw, ai_response)
                        return ai_response
                    ai_response = "Say 'update course [name] [price/description/content] to [value]', e.g., 'update course Python price to 16000'."
                    await self.generate_audio(ai_response, ai_response)
                    await save_interaction(self.employee_id, transcript_raw, ai_response)
                    return ai_response
                elif "status report" in transcript_lower:
                    ai_response = await generate_status_report()
                    ai_response += "\nWhat else can I help you with?"
                    await self.generate_audio(ai_response, ai_response)
                    await save_interaction(self.employee_id, transcript_raw, ai_response)
                    await save_hr_command("Generated status report", self.hr_user)
                    return ai_response
                elif "logout" in transcript_lower:
                    self.is_hr_authenticated = False
                    ai_response = "HR logout successful. How can I assist you now?"
                    await self.generate_audio(ai_response, ai_response)
                    await save_interaction(self.employee_id, transcript_raw, ai_response)
                    return ai_response

            intent = await detect_intent(transcript_lower)
            cache_key = intent if intent in self.response_cache else transcript_lower
            if cache_key in self.response_cache and not self.selected_course and intent != "help_choose":
                ai_response = self.response_cache[cache_key].format(name=self.employee_name)
                clean_response = clean_markdown(ai_response)
                await self.generate_audio(clean_response, ai_response)
                await save_interaction(self.employee_id, transcript_raw, ai_response)
                self.last_intent = intent
                if intent == "schedule":
                    self.counseling_context = "awaiting_choice"
                logging.info(f"Used cached response: {self.last_intent}")
                return clean_response

            existing_session = await fetch_session(self.employee_id)
            context = f"Employee: {self.employee_name}. "
            if existing_session:
                context += f"Has a {existing_session['mode']} session on {existing_session['date']} at {existing_session['time']}. "
            if self.selected_course:
                context += f"Selected course: {self.selected_course}. "
            context += """
            Respond concisely. Understand natural language (e.g., 'take admission' means schedule counseling). 
            For unrelated topics (e.g., sports), say: 'I can only assist with courses like Python, Java, Data Science, or Web Development.' 
            Ask 'Do you have a course in mind, or need help choosing?' only if no course specified. 
            If course mentioned, confirm and ask: 'Online or offline at our office?' 
            Online: immediate counseling with Gemini, using selected course details. If fails, autoschedule offline. 
            Offline: schedule at office. Use prior course context. 
            If help needed, list database courses, then ask to schedule. 
            Confirm offline: 'Okay [name], your offline session for [course] is scheduled for YYYY-MM-DD at HH:MM.' 
            Use only employee name. Dates 2025+. If unclear, ask gently. End with 'What else can I help you with?'
            """

            detected_course = detect_course(transcript_lower, self.selected_course)

            if intent == "course_details" or "tell me about" in transcript_lower:
                if detected_course:
                    course_details = await fetch_course_details(detected_course)
                    if course_details:
                        ai_response = (
                            f"{course_details['name']}: {course_details['description']} "
                            f"Duration: {course_details['duration']}, Fees: INR {course_details['fees']}. "
                            f"Covers: {course_details['content']}. "
                            "Want to schedule a session? What else can I help you with?"
                        )
                        await self.generate_audio(clean_markdown(ai_response), ai_response)
                        await save_interaction(self.employee_id, transcript_raw, ai_response)
                        self.last_intent = "course_details"
                        return clean_markdown(ai_response)
                    ai_response = f"Sorry, couldn't find {detected_course}. Try another course. What else can I help you with?"
                    await self.generate_audio(clean_markdown(ai_response), ai_response)
                    await save_interaction(self.employee_id, transcript_raw, ai_response)
                    return clean_markdown(ai_response)
                ai_response = "Which course would you like to know about, like Python or Java? What else can I help you with?"
                await self.generate_audio(clean_markdown(ai_response), ai_response)
                await save_interaction(self.employee_id, transcript_raw, ai_response)
                return clean_markdown(ai_response)

            if intent == "reschedule" and self.selected_course:
                ai_response = await reschedule_session(
                    self.employee_id, self.employee_name, self.employee_phone, 
                    self.sms_consent, transcript_en, self.counseling_mode or "offline", 
                    self.selected_course
                )
                await self.generate_audio(clean_markdown(ai_response), ai_response)
                await save_interaction(self.employee_id, transcript_raw, ai_response)
                self.last_intent = "reschedule"
                return clean_markdown(ai_response)

            if intent == "help_choose":
                ai_response = "I can suggest courses based on your interests! Are you into programming, data analysis, or web design? For example, Python is great for beginners, Data Science for analytics, or Web Development for building websites. What are your goals? What else can I help you with?"
                await self.generate_audio(clean_markdown(ai_response), ai_response)
                await save_interaction(self.employee_id, transcript_raw, ai_response)
                self.last_intent = "help_choose"
                return clean_markdown(ai_response)

            if self.counseling_context == "awaiting_choice":
                if intent == "schedule" or any(phrase in transcript_lower for phrase in ["just schedule", "book counseling", "schedule now", "admission", "enroll"]):
                    if detected_course:
                        self.selected_course = detected_course
                        self.counseling_context = "awaiting_mode"
                        ai_response = f"Okay {self.employee_name}, would you like this counseling session for {detected_course} to be online or offline at our office?"
                        await self.generate_audio(clean_markdown(ai_response), ai_response)
                        await save_interaction(self.employee_id, transcript_raw, ai_response)
                        logging.info(f"Awaiting mode (course: {detected_course})")
                        return clean_markdown(ai_response)
                    elif self.selected_course:
                        self.counseling_context = "awaiting_mode"
                        ai_response = f"Okay {self.employee_name}, would you like this counseling session for {self.selected_course} to be online or offline at our office?"
                        await self.generate_audio(clean_markdown(ai_response), ai_response)
                        await save_interaction(self.employee_id, transcript_raw, ai_response)
                        logging.info(f"Awaiting mode (course: {self.selected_course})")
                        return clean_markdown(ai_response)
                    else:
                        ai_response = "Do you have a course in mind, or would you like help choosing? What else can I help you with?"
                        await self.generate_audio(clean_markdown(ai_response), ai_response)
                        await save_interaction(self.employee_id, transcript_raw, ai_response)
                        return clean_markdown(ai_response)
                elif detected_course:
                    self.selected_course = detected_course
                    self.counseling_context = "awaiting_mode"
                    ai_response = f"Okay {self.employee_name}, would you like this counseling session for {detected_course} to be online or offline at our office?"
                    await self.generate_audio(clean_markdown(ai_response), ai_response)
                    await save_interaction(self.employee_id, transcript_raw, ai_response)
                    logging.info(f"Awaiting mode (course: {detected_course})")
                    return clean_markdown(ai_response)

            if self.counseling_context == "awaiting_mode":
                if "online" in transcript_lower:
                    self.counseling_mode = "online"
                    self.counseling_context = None
                    ai_response = await conduct_online_counseling(self.employee_name, self.selected_course, self.chat)
                    if "I'll schedule an offline session" in ai_response:
                        ai_response = await autoschedule_offline_session(
                            self.employee_id, self.employee_name, self.employee_phone, self.sms_consent, self.selected_course)
                    ai_response += " What else can I help you with?"
                    await self.generate_audio(clean_markdown(ai_response), ai_response)
                    await save_interaction(self.employee_id, transcript_raw, ai_response)
                    self.last_intent = "online_counseling"
                    return clean_markdown(ai_response)
                elif "offline" in transcript_lower:
                    self.counseling_mode = "offline"
                    self.counseling_context = "awaiting_schedule"
                    ai_response = f"Okay {self.employee_name}, when would you like your offline session for {self.selected_course}? Say a date and time, like May 15, 2025 at 11 AM."
                    await self.generate_audio(clean_markdown(ai_response), ai_response)
                    await save_interaction(self.employee_id, transcript_raw, ai_response)
                    self.last_intent = "schedule_offline"
                    return clean_markdown(ai_response)
                else:
                    ai_response = "Please say 'online' or 'offline' for your counseling session. What else can I help you with?"
                    await self.generate_audio(clean_markdown(ai_response), ai_response)
                    await save_interaction(self.employee_id, transcript_raw, ai_response)
                    return clean_markdown(ai_response)

            if self.counseling_context == "awaiting_schedule":
                ai_response = await save_session(
                    self.employee_id, self.employee_name, self.employee_phone,
                    self.sms_consent, transcript_en, self.counseling_mode, self.selected_course
                )
                await self.generate_audio(clean_markdown(ai_response), ai_response)
                await save_interaction(self.employee_id, transcript_raw, ai_response)
                self.last_intent = "schedule"
                self.counseling_context = None
                return clean_markdown(ai_response)

            # Handle unrelated queries
            unrelated_keywords = ["sport", "game", "exercise", "hobby", "activity"]
            if any(keyword in transcript_lower for keyword in unrelated_keywords):
                ai_response = f"I'm sorry, {self.employee_name}, I can only assist with courses like Python, Java, Data Science, or Web Development. Would you like to know more about these or schedule a counseling session? What else can I help you with?"
                await self.generate_audio(clean_markdown(ai_response), ai_response)
                await save_interaction(self.employee_id, transcript_raw, ai_response)
                return clean_markdown(ai_response)

            try:
                prompt = f"{context}\nUser query: {transcript_en}"
                response = await asyncio.to_thread(self.chat.send_message, prompt)
                ai_response = response.text.strip()
                if not ai_response:
                    ai_response = "I'm not sure what you mean. Try asking about courses, scheduling counseling, or course details. What else can I help you with?"
                await self.generate_audio(clean_markdown(ai_response), ai_response)
                await save_interaction(self.employee_id, transcript_raw, ai_response)
                return clean_markdown(ai_response)
            except Exception as e:
                logging.error(f"Gemini API error: {e}")
                ai_response = "I'm not sure what you mean. Try asking about courses, scheduling counseling, or course details. What else can I help you with?"
                await self.generate_audio(clean_markdown(ai_response), ai_response)
                await save_interaction(self.employee_id, transcript_raw, ai_response)
                return clean_markdown(ai_response)

        except Exception as e:
            logging.error(f"Error in generate_ai_response: {e}")
            ai_response = "Sorry, something went wrong. Please try again. What else can I help you with?"
            await self.generate_audio(clean_markdown(ai_response), ai_response)
            await save_interaction(self.employee_id, transcript_raw, ai_response)
            return clean_markdown(ai_response)

async def main():
    try:
        logging.info("Starting HR Assistant...")
        await create_tables()
        logging.info("Tables created, initializing assistant...")
        assistant = HR_Assistant()
        logging.info("Assistant initialized, starting transcription...")
        await assistant.get_employee_details()
        await assistant.start_transcription()
    except Exception as e:
        logging.error(f"Main execution failed: {e}")
        print(f"Emma: Error: {e}. Check hr_assistant.log for details.\n")

if __name__ == "__main__":
    asyncio.run(main())