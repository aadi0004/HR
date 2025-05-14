import os
from datetime import datetime
from dotenv import load_dotenv
import speech_recognition as sr
import google.generativeai as genai
import pyttsx3
import time
import re
import mysql.connector
from mysql.connector import Error
from twilio.rest import Client
import asyncio
import logging
import secrets
import string

# Setup logging
logging.basicConfig(filename='hr_assistant.log', level=logging.INFO, format='%(asctime)s - %(message)s')

# Load environment variables from .env
load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# MySQL configuration
MYSQL_CONFIG = {
    'host': 'localhost',
    'database': 'regex_software',
    'user': os.getenv("MYSQL_USER"),
    'password': os.getenv("MYSQL_PASSWORD")
}

# Twilio configuration
TWILIO_CONFIG = {
    'account_sid': os.getenv("TWILIO_ACCOUNT_SID"),
    'auth_token': os.getenv("TWILIO_AUTH_TOKEN"),
    'phone_number': os.getenv("TWILIO_PHONE_NUMBER")
}

# Response cache for common HR queries
RESPONSE_CACHE = {
    "schedule counseling": "Thanks, {name}! Do you have a course in mind, or would you like help choosing one?",
    "reschedule counseling": "Thanks, {name}! When would you like to move your counseling session to?",
    "course details": "Thanks, {name}! Which course would you like to know about, like Python or Java?",
    "available courses": "Thanks, {name}! We offer Python, Java, Data Science, and Web Development. Want details on any of these?",
    "just schedule": "Thanks, {name}! Let's book your counseling session. When are you free, like May 15, 2025 at 11 AM?"
}

# HR password for authentication
HR_PASSWORD = "regex123"

def clean_markdown(text):
    """Remove markdown formatting characters (e.g., *, **, _) from text."""
    text = re.sub(r'\*{1,2}(.*?)\*{1,2}', r'\1', text)
    text = re.sub(r'_(.*?)_', r'\1', text)
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    text = ' '.join(text.split())
    return text.strip()

def clean_name(name):
    """Clean employee name by removing prefixes like 'my name is'."""
    prefixes = r'^(my name is|this is|i am)\s+'
    name = re.sub(prefixes, '', name, flags=re.IGNORECASE).strip()
    return name

def generate_unique_code():
    """Generate a 6-character alphanumeric unique code."""
    characters = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(characters) for _ in range(6))

def detect_course(transcript_lower, prev_course=None):
    """Detect course name from transcript or use previous course context."""
    course_keywords = ["python", "java", "data science", "web development"]
    for keyword in course_keywords:
        if keyword in transcript_lower:
            course_map = {
                "python": "Python Programming",
                "java": "Java Development",
                "data science": "Data Science",
                "web development": "Web Development"
            }
            return course_map[keyword]
    return prev_course

async def connect_to_db():
    """Connect to MySQL database with buffered cursor."""
    try:
        connection = mysql.connector.connect(**MYSQL_CONFIG, buffered=True)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
        logging.error(f"MySQL connection error: {e}")
        return None
    return None

async def update_tables():
    """Update database schema to ensure required columns exist."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT VERSION()")
            mysql_version = cursor.fetchone()[0]
            print(f"MySQL Server Version: {mysql_version}")
            logging.info(f"MySQL Server Version: {mysql_version}")

            cursor.execute("SHOW COLUMNS FROM employees LIKE 'phone_number'")
            cursor.fetchall()
            if not cursor.rowcount:
                cursor.execute("ALTER TABLE employees ADD COLUMN phone_number VARCHAR(20)")
                print("Added phone_number column to employees table.")

            cursor.execute("SHOW COLUMNS FROM employees LIKE 'sms_consent'")
            cursor.fetchall()
            if not cursor.rowcount:
                cursor.execute("ALTER TABLE employees ADD COLUMN sms_consent BOOLEAN DEFAULT TRUE")
                print("Added sms_consent column to employees table.")

            cursor.execute("SHOW COLUMNS FROM employees LIKE 'unique_code'")
            cursor.fetchall()
            if not cursor.rowcount:
                cursor.execute("ALTER TABLE employees ADD COLUMN unique_code VARCHAR(6) UNIQUE")
                print("Added unique_code column to employees table.")

            cursor.execute("SHOW COLUMNS FROM courses LIKE 'fees'")
            cursor.fetchall()
            if not cursor.rowcount:
                cursor.execute("ALTER TABLE courses ADD COLUMN fees DECIMAL(10,2) DEFAULT 15000.00")
                print("Added fees column to courses table.")
            cursor.execute("SHOW COLUMNS FROM courses LIKE 'content'")
            cursor.fetchall()
            if not cursor.rowcount:
                cursor.execute("ALTER TABLE courses ADD COLUMN content TEXT")
                print("Added content column to courses table.")

            cursor.execute("SHOW COLUMNS FROM counseling_sessions LIKE 'mode'")
            cursor.fetchall()
            if not cursor.rowcount:
                cursor.execute("ALTER TABLE counseling_sessions ADD COLUMN mode ENUM('online', 'offline') NOT NULL DEFAULT 'offline'")
                print("Added mode column to counseling_sessions table.")

            cursor.execute("SHOW TABLES LIKE 'counseling_sessions'")
            cursor.fetchall()
            if cursor.rowcount:
                cursor.execute("SHOW INDEXES FROM counseling_sessions WHERE Key_name = 'idx_employee_id_created_at'")
                cursor.fetchall()
                if not cursor.rowcount:
                    cursor.execute("CREATE INDEX idx_employee_id_created_at ON counseling_sessions (employee_id, created_at)")
                    print("Added index on counseling_sessions(employee_id, created_at).")
            connection.commit()
        except Error as e:
            print(f"Error updating tables: {e}")
            logging.error(f"Error updating tables: {e}")
            connection.rollback()
        finally:
            cursor.close()
            connection.close()

async def create_tables():
    """Create necessary tables if they don't exist and update schema."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    phone_number VARCHAR(20),
                    sms_consent BOOLEAN DEFAULT TRUE,
                    unique_code VARCHAR(6) UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS counseling_sessions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    employee_id INT NOT NULL,
                    session_date DATE NOT NULL,
                    session_time TIME NOT NULL,
                    mode ENUM('online', 'offline') NOT NULL DEFAULT 'offline',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS employee_interactions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    employee_id INT NOT NULL,
                    employee_query TEXT NOT NULL,
                    ai_response TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (employee_id) REFERENCES employees(employee_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS courses (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    course_name VARCHAR(255) NOT NULL,
                    description TEXT,
                    duration VARCHAR(50),
                    fees DECIMAL(10,2) NOT NULL,
                    content TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS hr_commands (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    command_text TEXT NOT NULL,
                    executed_by VARCHAR(255) NOT NULL,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
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
                    "INSERT INTO courses (course_name, description, duration, fees, content) VALUES (%s, %s, %s, %s, %s)",
                    sample_courses
                )
            connection.commit()
            print("Tables ensured.")
            logging.info("Database tables ensured.")
            await update_tables()
        except Error as e:
            print(f"Error creating tables: {e}")
            logging.error(f"Error creating tables: {e}")
            connection.rollback()
        finally:
            cursor.close()
            connection.close()

async def save_interaction(employee_id, query, response):
    """Save employee interaction to MySQL database."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            query_sql = """
                INSERT INTO employee_interactions (employee_id, employee_query, ai_response)
                VALUES (%s, %s, %s)
            """
            cursor.execute(query_sql, (employee_id, query, response))
            connection.commit()
            print(f"Saved interaction for employee ID {employee_id}")
            logging.info(f"Saved interaction for employee ID {employee_id}: {query}")
        except Error as e:
            print(f"Error saving interaction: {e}")
            logging.error(f"Error saving interaction: {e}")
            connection.rollback()
        finally:
            cursor.close()
            connection.close()

async def save_hr_command(command_text, executed_by):
    """Save HR command to database for auditing."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("INSERT INTO hr_commands (command_text, executed_by) VALUES (%s, %s)",
                           (command_text, executed_by))
            connection.commit()
            print(f"Saved HR command: {command_text} by {executed_by}")
            logging.info(f"Saved HR command: {command_text} by {executed_by}")
        except Error as e:
            print(f"Error saving HR command: {e}")
            logging.error(f"Error saving HR command: {e}")
            connection.rollback()
        finally:
            cursor.close()
            connection.close()

async def get_or_create_employee(name, phone_number=None, sms_consent=True, unique_code=None):
    """Retrieve or create employee record in the database."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            if unique_code:
                cursor.execute("SELECT employee_id, name, phone_number, sms_consent, unique_code FROM employees WHERE unique_code = %s", (unique_code,))
                result = cursor.fetchone()
                if result:
                    employee_id, stored_name, stored_phone, stored_consent, stored_code = result
                    update_needed = False
                    if phone_number and stored_phone != phone_number:
                        update_needed = True
                    if sms_consent != stored_consent:
                        update_needed = True
                    if update_needed:
                        cursor.execute("UPDATE employees SET phone_number = %s, sms_consent = %s WHERE employee_id = %s",
                                       (phone_number or stored_phone, sms_consent, employee_id))
                        connection.commit()
                    return employee_id, stored_name, stored_phone or phone_number, stored_consent, stored_code
            cursor.execute("SELECT employee_id, name, phone_number, sms_consent, unique_code FROM employees WHERE name = %s", (name,))
            result = cursor.fetchone()
            if result:
                employee_id, stored_name, stored_phone, stored_consent, stored_code = result
                update_needed = False
                if phone_number and stored_phone != phone_number:
                    update_needed = True
                if sms_consent != stored_consent:
                    update_needed = True
                if update_needed:
                    cursor.execute("UPDATE employees SET phone_number = %s, sms_consent = %s WHERE employee_id = %s",
                                   (phone_number or stored_phone, sms_consent, employee_id))
                    connection.commit()
                return employee_id, stored_name, stored_phone or phone_number, stored_consent, stored_code
            else:
                unique_code = unique_code or generate_unique_code()
                cursor.execute("INSERT INTO employees (name, phone_number, sms_consent, unique_code) VALUES (%s, %s, %s, %s)",
                               (name, phone_number, sms_consent, unique_code))
                connection.commit()
                cursor.execute("SELECT LAST_INSERT_ID()")
                employee_id = cursor.fetchone()[0]
                return employee_id, name, phone_number, sms_consent, unique_code
        except Error as e:
            print(f"Error managing employee: {e}")
            logging.error(f"Error managing employee: {e}")
            connection.rollback()
        finally:
            cursor.close()
            connection.close()
    return None, name, None, True, None

async def fetch_session(employee_id):
    """Fetch the latest counseling session for an employee."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("""
                SELECT id, session_date, session_time, mode
                FROM counseling_sessions
                WHERE employee_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (employee_id,))
            result = cursor.fetchone()
            if result:
                print(f"Fetched session for employee ID {employee_id}: ID={result[0]}, Date={result[1]}, Time={result[2]}, Mode={result[3]}")
                logging.info(f"Fetched session for employee ID {employee_id}: ID={result[0]}, Date={result[1]}, Time={result[2]}, Mode={result[3]}")
                return {'id': result[0], 'date': result[1], 'time': result[2], 'mode': result[3]}
            print(f"No session found for employee ID {employee_id}")
            return None
        except Error as e:
            print(f"Error fetching session: {e}")
            logging.error(f"Error fetching session: {e}")
            connection.rollback()
        finally:
            cursor.close()
            connection.close()
    return None

async def fetch_course_details(course_name):
    """Fetch details of a specific course from the database."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("""
                SELECT id, course_name, description, duration, fees, content
                FROM courses
                WHERE course_name LIKE %s
                LIMIT 1
            """, (f"%{course_name}%",))
            result = cursor.fetchone()
            if result:
                print(f"Fetched course: {result[1]}")
                logging.info(f"Fetched course: {result[1]}")
                return {
                    'id': result[0],
                    'name': result[1],
                    'description': result[2],
                    'duration': result[3],
                    'fees': result[4],
                    'content': result[5]
                }
            print(f"No course found matching: {course_name}")
            return None
        except Error as e:
            print(f"Error fetching course: {e}")
            logging.error(f"Error fetching course: {e}")
            connection.rollback()
        finally:
            cursor.close()
            connection.close()
    return None

async def fetch_all_courses():
    """Fetch all courses from the database for counseling suggestions."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("""
                SELECT course_name, description, duration, fees, content
                FROM courses
            """)
            results = cursor.fetchall()
            courses = [
                {
                    'name': row[0],
                    'description': row[1],
                    'duration': row[2],
                    'fees': row[3],
                    'content': row[4]
                }
                for row in results
            ]
            print(f"Fetched {len(courses)} courses for suggestion")
            logging.info(f"Fetched {len(courses)} courses for suggestion")
            return courses
        except Error as e:
            print(f"Error fetching courses: {e}")
            logging.error(f"Error fetching courses: {e}")
            return []
        finally:
            cursor.close()
            connection.close()
    return []

async def fetch_employee_interactions(unique_code=None, employee_name=None):
    """Fetch interaction history for an employee by unique code or name."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            if unique_code:
                query = """
                    SELECT ei.employee_query, ei.ai_response, ei.created_at
                    FROM employee_interactions ei
                    JOIN employees e ON ei.employee_id = e.employee_id
                    WHERE e.unique_code = %s
                    ORDER BY ei.created_at DESC
                    LIMIT 5
                """
                cursor.execute(query, (unique_code,))
            else:
                query = """
                    SELECT ei.employee_query, ei.ai_response, ei.created_at
                    FROM employee_interactions ei
                    JOIN employees e ON ei.employee_id = e.employee_id
                    WHERE e.name LIKE %s
                    ORDER BY ei.created_at DESC
                    LIMIT 5
                """
                cursor.execute(query, (f"%{employee_name}%",))
            results = cursor.fetchall()
            interactions = [
                f"Query: {row[0]}, Response: {row[1]}, Time: {row[2]}"
                for row in results
            ]
            identifier = unique_code or employee_name
            print(f"Fetched {len(interactions)} interactions for {identifier}")
            logging.info(f"Fetched {len(interactions)} interactions for {identifier}")
            return interactions
        except Error as e:
            print(f"Error fetching interactions: {e}")
            logging.error(f"Error fetching interactions: {e}")
            return []
        finally:
            cursor.close()
            connection.close()
    return []

async def generate_status_report():
    """Generate a report of scheduled sessions."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM counseling_sessions")
            sessions = cursor.fetchone()[0]
            report = f"Total counseling sessions: {sessions}"
            print("Generated status report")
            logging.info("Generated status report")
            return report
        except Error as e:
            print(f"Error generating status report: {e}")
            logging.error(f"Error generating status report: {e}")
            return "Error generating report."
        finally:
            cursor.close()
            connection.close()
    return "Database connection failed."

async def update_course_details(course_name, field, value, executed_by):
    """Update course details (price, description, content)."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            valid_fields = {'price': 'fees', 'description': 'description', 'content': 'content'}
            if field.lower() not in valid_fields:
                return "Invalid field. Use price, description, or content."
            db_field = valid_fields[field.lower()]
            if db_field == 'fees':
                value = float(value)
                if value <= 12000:
                    return "Course price must be above 12,000 INR."
            cursor.execute(f"UPDATE courses SET {db_field} = %s WHERE course_name LIKE %s",
                           (value, f"%{course_name}%"))
            if cursor.rowcount > 0:
                connection.commit()
                await save_hr_command(f"Updated {field} of {course_name} to {value}", executed_by)
                print(f"Updated {field} for {course_name}")
                logging.info(f"Updated {field} for {course_name}")
                return f"Updated {field} for {course_name} successfully."
            else:
                return "Course not found."
        except Error as e:
            print(f"Error updating course: {e}")
            logging.error(f"Error updating course: {e}")
            connection.rollback()
            return "Error updating course."
        finally:
            cursor.close()
            connection.close()
    return "Database connection failed."

async def check_availability(session_date, session_time):
    """Check if a counseling session slot is available."""
    connection = await connect_to_db()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM counseling_sessions
                WHERE session_date = %s AND session_time = %s
            """, (session_date, session_time))
            count = cursor.fetchone()[0]
            return count == 0
        except Error as e:
            print(f"Error checking availability: {e}")
            logging.error(f"Error checking availability: {e}")
            return False
        finally:
            cursor.close()
            connection.close()
    return False

async def send_sms(employee_name, employee_phone, message_body):
    """Send SMS with robust error handling."""
    if not employee_phone or not TWILIO_CONFIG['phone_number']:
        print("Missing phone number or Twilio configuration.")
        logging.error("Missing phone number or Twilio configuration.")
        return False
    to_number = f"+91{employee_phone}" if not employee_phone.startswith('+') else employee_phone
    try:
        twilio_client = Client(TWILIO_CONFIG['account_sid'], TWILIO_CONFIG['auth_token'])
        message = twilio_client.messages.create(
            body=f"Hi {employee_name}, {message_body} Contact HR at (555) 987-6543 for assistance. -Regex Software HR",
            from_=TWILIO_CONFIG['phone_number'],
            to=to_number
        )
        print(f"SMS sent to {employee_phone} (SID: {message.sid})")
        logging.info(f"SMS sent to {employee_phone} (SID: {message.sid})")
        return True
    except Exception as e:
        print(f"Failed to send SMS: {e}")
        logging.error(f"Failed to send SMS: {e}")
        return False

async def send_session_message(employee_name, employee_phone, sms_consent, session_date, session_time, mode, is_reschedule=False):
    """Send counseling session confirmation via SMS."""
    if employee_phone and sms_consent and session_date and session_time:
        action = "rescheduled" if is_reschedule else "scheduled"
        location = "at our office" if mode == "offline" else "online"
        body = f"your {mode} counseling session {location} is {action} for {session_date} at {session_time}."
        success = await send_sms(employee_name, employee_phone, body)
        if not success:
            return f"Your {mode} session {location} is {action} for {session_date} at {session_time}, but I couldn't send an SMS confirmation."
        return f"Your {mode} session {location} is {action} for {session_date} at {session_time}."
    print("No valid phone number, SMS consent, or session details provided. Skipping message.")
    return f"Your {mode} session {location} is booked for {session_date} at {session_time}."

async def conduct_online_counseling(employee_name, course_name, chat_session):
    """Conduct an online counseling session using Gemini and persuade enrollment."""
    course_details = await fetch_course_details(course_name)
    if not course_details:
        return f"Sorry, I couldn't find details for {course_name}. Would you like to discuss another course?"

    counseling_prompt = f"""
    You are Emma, a professional and friendly HR assistant at Regex Software. Conduct an engaging online counseling session for {employee_name} about the {course_name} course. Use the following details:
    - Description: {course_details['description']}
    - Duration: {course_details['duration']}
    - Fees: INR {course_details['fees']}
    - Content: {course_details['content']}
    
    Highlight the course's benefits, such as practical skills, career opportunities, and hands-on projects. Be concise, persuasive, and professional. Address the user by name, ask about their goals, and explain how the course aligns with them. End by encouraging enrollment and offering to schedule a follow-up session to finalize it. Conclude with: 'What are your thoughts on this, {employee_name}? Would you like to proceed with enrollment or schedule a follow-up session?'
    """

    try:
        response = await asyncio.to_thread(chat_session.send_message, counseling_prompt)
        counseling_response = response.text.strip()
        clean_response = clean_markdown(counseling_response)
        return clean_response
    except Exception as e:
        print(f"Error conducting online counseling: {e}")
        logging.error(f"Error conducting online counseling: {e}")
        return f"Sorry, I couldn't conduct the online session for {course_name}. Would you like to try again or schedule an offline session?"

async def save_session(employee_id, employee_name, employee_phone, sms_consent, response, mode, course_name):
    """Parse AI response and save new counseling session details to database."""
    session_pattern = r"(?:scheduled|booked|set|rescheduled)\s*(?:for|on|at)?\s*(\d{4}-\d{2}-\d{2})\s*(?:at)?\s*(\d{2}:\d{2}(?:\s*(?:AM|PM))?)"
    match = re.search(session_pattern, response, re.IGNORECASE)
    
    if match:
        session_date = match.group(1)
        session_time = match.group(2)
        try:
            session_year = int(session_date.split('-')[0])
            if session_year < 2025:
                print(f"Invalid year {session_year} detected, correcting to 2025")
                session_date = f"2025{session_date[4:]}"
        except ValueError:
            print("Invalid date format detected")
            return "Invalid date format. Please say it again, like May 15, 2025 at 11 AM."

        if "AM" in session_time.upper() or "PM" in session_time.upper():
            try:
                time_obj = datetime.strptime(session_time, "%I:%M %p")
                session_time = time_obj.strftime("%H:%M")
            except ValueError:
                print("Invalid time format detected")
                return "Invalid time format. Please say it again, like 11:00 AM."

        print(f"Regex matched: date={session_date}, time={session_time}, mode={mode}")
        
        if await check_availability(session_date, session_time):
            connection = await connect_to_db()
            if connection:
                try:
                    cursor = connection.cursor()
                    query_sql = """
                        INSERT INTO counseling_sessions (employee_id, session_date, session_time, mode)
                        VALUES (%s, %s, %s, %s)
                    """
                    cursor.execute(query_sql, (employee_id, session_date, session_time, mode))
                    if cursor.rowcount > 0:
                        connection.commit()
                        print(f"Saved new {mode} session for employee ID {employee_id} on {session_date} at {session_time}")
                        logging.info(f"Saved new {mode} session for employee ID {employee_id} on {session_date} at {session_time}")
                        if mode == "online":
                            confirmation = await conduct_online_counseling(employee_name, course_name, chat_session)
                            await send_session_message(employee_name, employee_phone, sms_consent, session_date, session_time, mode)
                            return confirmation
                        else:
                            confirmation = await send_session_message(employee_name, employee_phone, sms_consent, session_date, session_time, mode)
                            return confirmation
                    else:
                        print(f"Failed to save session for employee ID {employee_id}: No rows affected.")
                        connection.rollback()
                        return "Sorry, I couldn't book your session. Please try again."
                except Error as e:
                    print(f"Error saving session: {e}")
                    logging.error(f"Error saving session: {e}")
                    connection.rollback()
                    return "Sorry, I couldn't book your session due to a database error."
                finally:
                    cursor.close()
                    connection.close()
        else:
            return "That time slot is taken. Please choose another date or time, like May 16, 2025 at 11 AM."
    else:
        print(f"No session details detected in response: {response}")
        return "I didn't catch the date and time. Could you say it again, like May 15, 2025 at 11 AM?"

async def reschedule_session(employee_id, employee_name, employee_phone, sms_consent, response, mode, course_name):
    """Parse AI response and reschedule existing counseling session."""
    session_pattern = r"(?:re)?scheduled\s*(?:for|to|on|at)?\s*(\d{4}-\d{2}-\d{2})\s*(?:at)?\s*(\d{2}:\d{2}(?:\s*(?:AM|PM))?)"
    match = re.search(session_pattern, response, re.IGNORECASE)
    
    if match:
        session_date = match.group(1)
        session_time = match.group(2)
        try:
            session_year = int(session_date.split('-')[0])
            if session_year < 2025:
                print(f"Invalid year {session_year} detected, correcting to 2025")
                session_date = f"2025{session_date[4:]}"
        except ValueError:
            print("Invalid date format detected")
            return "Invalid date format. Please say it again, like May 15, 2025 at 11 AM."

        if "AM" in session_time.upper() or "PM" in session_time.upper():
            try:
                time_obj = datetime.strptime(session_time, "%I:%M %p")
                session_time = time_obj.strftime("%H:%M")
            except ValueError:
                print("Invalid time format detected")
                return "Invalid time format. Please say it again, like 11:00 AM."

        print(f"Regex matched for reschedule: date={session_date}, time={session_time}, mode={mode}")
        
        if await check_availability(session_date, session_time):
            existing_session = await fetch_session(employee_id)
            if existing_session:
                connection = await connect_to_db()
                if connection:
                    try:
                        cursor = connection.cursor()
                        query_sql = """
                            UPDATE counseling_sessions
                            SET session_date = %s, session_time = %s, mode = %s, created_at = CURRENT_TIMESTAMP
                            WHERE id = %s AND employee_id = %s
                        """
                        cursor.execute(query_sql, (session_date, session_time, mode, existing_session['id'], employee_id))
                        if cursor.rowcount > 0:
                            connection.commit()
                            print(f"Rescheduled {mode} session for employee ID {employee_id} to {session_date} at {session_time}")
                            logging.info(f"Rescheduled {mode} session for employee ID {employee_id} to {session_date} at {session_time}")
                            if mode == "online":
                                confirmation = await conduct_online_counseling(employee_name, course_name, chat_session)
                                await send_session_message(employee_name, employee_phone, sms_consent, session_date, session_time, mode, is_reschedule=True)
                                return confirmation
                            else:
                                confirmation = await send_session_message(employee_name, employee_phone, sms_consent, session_date, session_time, mode, is_reschedule=True)
                                return confirmation
                        else:
                            print(f"Failed to update session for employee ID {employee_id}: No rows affected.")
                            connection.rollback()
                            return "Sorry, I couldn't reschedule your session. Please try again."
                    except Error as e:
                        print(f"Error rescheduling session: {e}")
                        logging.error(f"Error rescheduling session: {e}")
                        connection.rollback()
                        return "Sorry, I couldn't reschedule your session due to a database error."
                    finally:
                        cursor.close()
                        connection.close()
            else:
                print("No existing session found. Saving as new session.")
                return await save_session(employee_id, employee_name, employee_phone, sms_consent, response, mode, course_name)
        else:
            return "That time slot is taken. Please choose another date or time, like May 16, 2025 at 11 AM."
    else:
        print(f"No reschedule details detected in response: {response}")
        return "I didn't catch the new date and time. Could you say it again, like May 15, 2025 at 11 AM?"

class HR_Assistant:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()
        self.recognizer.energy_threshold = 4000
        self.recognizer.dynamic_energy_threshold = True

        self.model = genai.GenerativeModel("gemini-1.5-flash")
        self.chat = self.model.start_chat(history=[
            {"role": "user", "parts": [
                "You are Emma, a friendly and professional HR assistant at Regex Software. Assist with scheduling counseling sessions, providing course details, and HR tasks. For counseling, ask if they have a course in mind or need help choosing, using: 'Do you have a course in mind, or would you like help choosing one?' only if no course is specified. If they mention a course (e.g., 'Data Science', 'Python') in any sentence, recognize it, confirm the course, and ask if they prefer online or offline counseling. For offline, schedule at the office. For online, conduct a counseling session with Gemini, persuading enrollment. Use prior context if a course was mentioned earlier. If they need help, list courses ONLY from the database with name, description, fees, duration, and content, then ask to schedule. Confirm scheduling with 'Okay [name], your [online/offline] counseling session for [course] is scheduled for YYYY-MM-DD at HH:MM.' For rescheduling, confirm with 'Okay [name], your [online/offline] counseling session is rescheduled for YYYY-MM-DD at HH:MM.' For HR users (after 'HR login' and password), handle commands like viewing interactions by unique code or name, updating course details, or generating reports. Use only the employee's name in responses, not their phone number. If input is unclear, ask gently, e.g., 'Could you say that again, like the date or course name?' Always end with 'What else can I help you with?' Ensure dates are in 2025 or later."
            ]},
            {"role": "model", "parts": ["Hello, I'm Emma, your HR assistant at Regex Software. How can I assist you today?"]}])
        
        self.tts_engine = pyttsx3.init()
        self.tts_engine.setProperty('rate', 180)
        self.tts_engine.setProperty('volume', 0.8)
        voices = self.tts_engine.getProperty('voices')
        for voice in voices:
            if "Zira" in voice.name:
                self.tts_engine.setProperty('voice', voice.id)
                break
        else:
            for voice in voices:
                if "female" in voice.name.lower():
                    self.tts_engine.setProperty('voice', voice.id)
                    break

        self.employee_name = "Unknown"
        self.employee_id = None
        self.employee_phone = None
        self.sms_consent = True
        self.unique_code = None
        self.last_intent = None
        self.last_response = None
        self.counseling_context = None
        self.selected_course = None
        self.counseling_mode = None
        self.is_hr_authenticated = False
        self.hr_user = "HR_Admin"

        self.audio_cache = {}
        self.response_cache = RESPONSE_CACHE
        self.audio_failure_count = 0

    def cleanup(self):
        """Explicitly stop the TTS engine."""
        try:
            self.tts_engine.stop()
        except Exception as e:
            print(f"Error stopping TTS engine: {e}")
            logging.error(f"Error stopping TTS engine: {e}")

    async def get_employee_details(self):
        """Prompt for and capture unique code, employee name, and phone number via speech with retry."""
        max_attempts = 3
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=1.5)

            # Ask if user has a unique code
            print("Prompting: Do you have a unique code from a previous session?")
            await self.generate_audio("Do you have a unique code from a previous session? If yes, please say it. If not, say 'new user'.", 
                                     "Do you have a unique code from a previous session? If yes, please say it. If not, say 'new user'.")
            for attempt in range(max_attempts):
                try:
                    audio = self.recognizer.listen(source, timeout=8, phrase_time_limit=8)
                    response = self.recognizer.recognize_google(audio).strip().lower()
                    print(f"Raw code input: {response}")
                    logging.info(f"Raw code input: {response}")
                    if "new user" in response:
                        self.unique_code = None
                        break
                    elif re.match(r'^[A-Z0-9]{6}$', response.upper()):
                        self.unique_code = response.upper()
                        employee_id, name, phone, sms_consent, stored_code = await get_or_create_employee("Temporary", unique_code=self.unique_code)
                        if employee_id:
                            self.employee_id = employee_id
                            self.employee_name = name
                            self.employee_phone = phone
                            self.sms_consent = sms_consent
                            self.unique_code = stored_code
                            print(f"Retrieved employee with code {self.unique_code}: {self.employee_name}")
                            await self.generate_audio(f"Welcome back, {self.employee_name}! How can I assist you today?",
                                                     f"Welcome back, {self.employee_name}! How can I assist you today?")
                            return
                        else:
                            print("Invalid code. Prompting for new user.")
                            self.unique_code = None
                            break
                    else:
                        print("Invalid code format detected.")
                except sr.WaitTimeoutError:
                    print("Timeout: No speech detected.")
                except sr.UnknownValueError:
                    print("Could not understand audio.")
                except sr.RequestError as e:
                    print(f"Google API error: {e}")
                    logging.error(f"Google API error: {e}")
                except Exception as e:
                    print(f"Error capturing code: {e}")
                    logging.error(f"Error capturing code: {e}")

                if attempt < max_attempts - 1:
                    await self.generate_audio("I didn't catch your code. Please say your 6-character code or say 'new user'.",
                                             "I didn't catch your code. Please say your 6-character code or say 'new user'.")
                    time.sleep(0.1)

            # If no valid code, prompt for name
            for attempt in range(max_attempts):
                print(f"Attempt {attempt + 1}/{max_attempts}: Please say your name.")
                await self.generate_audio("Please say your name.", "Please say your name.")
                try:
                    audio = self.recognizer.listen(source, timeout=8, phrase_time_limit=8)
                    name = self.recognizer.recognize_google(audio).strip()
                    print(f"Raw name input: {name}")
                    logging.info(f"Raw name input: {name}")
                    if name:
                        self.employee_name = clean_name(name)
                        print(f"Employee name: {self.employee_name}")
                        break
                    else:
                        print("No name detected.")
                except sr.WaitTimeoutError:
                    print("Timeout: No speech detected.")
                except sr.UnknownValueError:
                    print("Could not understand audio.")
                except sr.RequestError as e:
                    print(f"Google API error: {e}")
                    logging.error(f"Google API error: {e}")
                except Exception as e:
                    print(f"Error capturing name: {e}")
                    logging.error(f"Error capturing name: {e}")

                if attempt < max_attempts - 1:
                    await self.generate_audio("I didn't catch your name. Could you say it again?", 
                                             "I didn't catch your name. Could you say it again?")
                    time.sleep(0.1)

            if self.employee_name == "Unknown":
                print("No name detected after retries, using 'Unknown'.")
                await self.generate_audio("I couldn't get your name, so I'll use 'Unknown' for now.", 
                                         "I couldn't get your name, so I'll use 'Unknown' for now.")

            self.employee_id, self.employee_name, self.employee_phone, self.sms_consent, self.unique_code = await get_or_create_employee(
                self.employee_name, self.employee_phone, self.sms_consent, self.unique_code)

            # Provide unique code to new users
            if self.unique_code:
                await self.generate_audio(f"Your unique code is {self.unique_code}. Please save it to reference your conversations later.",
                                         f"Your unique code is {self.unique_code}. Please save it to reference your conversations later.")

            # Prompt for phone number if not already set
            if not self.employee_phone:
                for attempt in range(max_attempts):
                    print(f"Attempt {attempt + 1}/{max_attempts}: Please say your phone number.")
                    await self.generate_audio("Please say your 10-digit phone number.", 
                                             "Please say your 10-digit phone number.")
                    try:
                        audio = self.recognizer.listen(source, timeout=8, phrase_time_limit=8)
                        phone = self.recognizer.recognize_google(audio).strip()
                        print(f"Raw phone input: {phone}")
                        logging.info(f"Raw phone input: {phone}")
                        phone = re.sub(r'\D', '', phone)
                        if len(phone) >= 10:
                            self.employee_phone = phone[:10]
                            print(f"Employee phone: {self.employee_phone}")
                            self.employee_id, _, self.employee_phone, self.sms_consent, self.unique_code = await get_or_create_employee(
                                self.employee_name, self.employee_phone, self.sms_consent, self.unique_code)
                            break
                        else:
                            print("Invalid phone number format detected.")
                    except sr.WaitTimeoutError:
                        print("Timeout: No speech detected.")
                    except sr.UnknownValueError:
                        print("Could not understand audio.")
                    except sr.RequestError as e:
                        print(f"Google API error: {e}")
                        logging.error(f"Google API error: {e}")
                    except Exception as e:
                        print(f"Error capturing phone number: {e}")
                        logging.error(f"Error capturing phone number: {e}")

                    if attempt < max_attempts - 1:
                        await self.generate_audio("I need a 10-digit phone number. Could you say it again?", 
                                                 "I need a 10-digit phone number. Could you say it again?")
                        time.sleep(0.1)

        await self.generate_audio(f"Hi, {self.employee_name}! How can I assist you today?", 
                                 f"Hi, {self.employee_name}! How can I assist you today?")

    async def start_transcription(self):
        """Listen for employee speech and generate AI responses."""
        print("Listening... Speak now.")
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=1.5)
            while True:
                try:
                    audio = self.recognizer.listen(source, timeout=8, phrase_time_limit=8)
                    try:
                        transcript = self.recognizer.recognize_google(audio)
                        print(f"Raw transcript: {transcript}")
                        logging.info(f"Raw transcript: {transcript}")
                        if transcript.strip():
                            print(f"\nEmployee: {transcript}\n")
                            self.audio_failure_count = 0
                            await self.generate_ai_response(transcript)
                        else:
                            print("No speech detected, continuing to listen...")
                    except sr.UnknownValueError:
                        self.audio_failure_count += 1
                        print("Could not understand audio.")
                        if self.audio_failure_count >= 3:
                            await self.generate_audio("I'm having trouble hearing you. Please try again or say 'schedule counseling'.",
                                                     "I'm having trouble hearing you. Please try again or say 'schedule counseling'.")
                            self.audio_failure_count = 0
                    except sr.RequestError as e:
                        print(f"Google API error: {e}, continuing to listen...")
                        logging.error(f"Google API error: {e}")
                    except Exception as e:
                        print(f"Error processing audio: {e}")
                        logging.error(f"Error processing audio: {e}")
                except KeyboardInterrupt:
                    print("\nStopping transcription...")
                    break
                except Exception as e:
                    print(f"Error during transcription: {e}")
                    logging.error(f"Error during transcription: {e}")
                    time.sleep(0.1)

    async def generate_ai_response(self, transcript):
        """Generate AI response and handle scheduling/rescheduling/course inquiries/HR tasks."""
        try:
            if self.employee_id and self.employee_name != "Unknown":
                transcript_lower = transcript.lower().strip()

                # Handle HR login
                if "hr login" in transcript_lower:
                    if HR_PASSWORD in transcript_lower:
                        self.is_hr_authenticated = True
                        self.hr_user = self.employee_name
                        ai_response = "HR login successful. You can now view interactions, update courses, or generate reports. What would you like to do?"
                        await self.generate_audio(ai_response, ai_response)
                        await save_interaction(self.employee_id, transcript, ai_response)
                        return
                    else:
                        ai_response = "Please say the correct HR password."
                        await self.generate_audio(ai_response, ai_response)
                        await save_interaction(self.employee_id, transcript, ai_response)
                        return

                # HR commands
                if self.is_hr_authenticated:
                    if "view interactions" in transcript_lower:
                        code_match = re.search(r'code\s+([A-Z0-9]{6})', transcript_lower)
                        name_match = re.search(r'for\s+(.+?)(?:\s+code|$)', transcript_lower)
                        if code_match:
                            interactions = await fetch_employee_interactions(unique_code=code_match.group(1))
                            identifier = f"code {code_match.group(1)}"
                        elif name_match:
                            interactions = await fetch_employee_interactions(employee_name=name_match.group(1).strip())
                            identifier = f"name {name_match.group(1).strip()}"
                        else:
                            interactions = await fetch_employee_interactions(employee_name=self.employee_name)
                            identifier = f"name {self.employee_name}"
                        ai_response = f"Here are recent interactions for {identifier}:\n" + "\n".join(interactions) if interactions else f"No interactions found for {identifier}."
                        ai_response += "\nWhat else can I help you with?"
                        await self.generate_audio(ai_response, ai_response)
                        await save_interaction(self.employee_id, transcript, ai_response)
                        await save_hr_command(f"Viewed interactions for {identifier}", self.hr_user)
                        return
                    elif "update course" in transcript_lower:
                        match = re.search(r"update course (\w+) (price|description|content) to (.+)", transcript_lower, re.IGNORECASE)
                        if match:
                            course_name, field, value = match.groups()
                            ai_response = await update_course_details(course_name, field, value, self.hr_user)
                            ai_response += " What else can I help you with?"
                            await self.generate_audio(ai_response, ai_response)
                            await save_interaction(self.employee_id, transcript, ai_response)
                            return
                        else:
                            ai_response = "Please say 'update course [name] [price/description/content] to [value]', like 'update course Python price to 16000'."
                            await self.generate_audio(ai_response, ai_response)
                            await save_interaction(self.employee_id, transcript, ai_response)
                            return
                    elif "status report" in transcript_lower:
                        ai_response = await generate_status_report()
                        ai_response += "\nWhat else can I help you with?"
                        await self.generate_audio(ai_response, ai_response)
                        await save_interaction(self.employee_id, transcript, ai_response)
                        await save_hr_command("Generated status report", self.hr_user)
                        return
                    elif "logout" in transcript_lower:
                        self.is_hr_authenticated = False
                        ai_response = "HR logout successful. How can I assist you now?"
                        await self.generate_audio(ai_response, ai_response)
                        await save_interaction(self.employee_id, transcript, ai_response)
                        return

                # Regular user commands
                cache_key = transcript_lower
                if cache_key in self.response_cache and not self.selected_course:
                    ai_response = self.response_cache[cache_key].format(name=self.employee_name)
                    clean_response = clean_markdown(ai_response)
                    await self.generate_audio(clean_response, ai_response)
                    await save_interaction(self.employee_id, transcript, ai_response)
                    self.last_intent = "reschedule" if "reschedule" in cache_key else "schedule" if "schedule" in cache_key else "course"
                    if cache_key == "schedule counseling":
                        self.counseling_context = "awaiting_choice"
                    print(f"Using cached response. Intent set to: {self.last_intent}")
                    logging.info(f"Used cached response for intent: {self.last_intent}")
                    return

                existing_session = await fetch_session(self.employee_id)
                context = f"The employee's name is {self.employee_name}. "
                if existing_session:
                    context += f"They have a {existing_session['mode']} counseling session on {existing_session['date']} at {existing_session['time']}. "
                if self.selected_course:
                    context += f"They have selected the {self.selected_course} course. "
                context += """
                Respond concisely. For counseling, ask if they have a course in mind or need help choosing only if no course is specified or previously selected. 
                If they mention a course (e.g., 'Data Science', 'Python') in any sentence, recognize it, confirm the course, and ask: 'Would you like this counseling session to be online or offline at our office?' 
                For offline, schedule at the office. For online, conduct a counseling session with Gemini, persuading enrollment. 
                Use prior context if a course was mentioned earlier. If they need help, list courses ONLY from the database with name, description, fees, duration, and content, then ask to schedule. 
                Confirm scheduling with 'Okay [name], your [online/offline] counseling session for [course] is scheduled for YYYY-MM-DD at HH:MM.' 
                Use only the employee's name, not their phone number. Ensure dates are in 2025 or later. 
                If input is unclear, ask gently, e.g., 'Could you say that again, like the date or course name?'
                """

                detected_course = detect_course(transcript_lower, self.selected_course)

                # Handle counseling context
                if self.counseling_context == "awaiting_choice":
                    if any(phrase in transcript_lower for phrase in ["just schedule", "book counseling", "schedule now", "schedule only session"]):
                        if detected_course:
                            self.selected_course = detected_course
                            self.counseling_context = "awaiting_mode"
                            ai_response = f"Okay {self.employee_name}, would you like this counseling session for {detected_course} to be online or offline at our office?"
                            clean_response = clean_markdown(ai_response)
                            await self.generate_audio(clean_response, ai_response)
                            await save_interaction(self.employee_id, transcript, ai_response)
                            print(f"Intent: awaiting mode (course: {detected_course})")
                            logging.info(f"Intent: awaiting mode (course: {detected_course})")
                            return
                        elif self.selected_course:
                            self.counseling_context = "awaiting_mode"
                            ai_response = f"Okay {self.employee_name}, would you like this counseling session for {self.selected_course} to be online or offline at our office?"
                            clean_response = clean_markdown(ai_response)
                            await self.generate_audio(clean_response, ai_response)
                            await save_interaction(self.employee_id, transcript, ai_response)
                            print(f"Intent: awaiting mode (using prior course: {self.selected_course})")
                            logging.info(f"Intent: awaiting mode (using prior course: {self.selected_course})")
                            return
                        else:
                            ai_response = "I didn't catch the course name. Could you say it again, like 'Data Science' or 'Python'?"
                            clean_response = clean_markdown(ai_response)
                            await self.generate_audio(clean_response, ai_response)
                            await save_interaction(self.employee_id, transcript, ai_response)
                            print(f"Intent: awaiting course clarification")
                            logging.info(f"Intent: awaiting course clarification")
                            return
                    elif detected_course:
                        self.selected_course = detected_course
                        self.counseling_context = "awaiting_mode"
                        ai_response = f"Okay {self.employee_name}, would you like this counseling session for {detected_course} to be online or offline at our office?"
                        clean_response = clean_markdown(ai_response)
                        await self.generate_audio(clean_response, ai_response)
                        await save_interaction(self.employee_id, transcript, ai_response)
                        print(f"Intent: awaiting mode (course: {detected_course})")
                        logging.info(f"Intent: awaiting mode (course: {detected_course})")
                        return
                    elif any(phrase in transcript_lower for phrase in ["need counseling", "help choosing", "suggest courses"]):
                        self.counseling_context = "course_suggestion"
                        courses = await fetch_all_courses()
                        if courses:
                            ai_response = f"Okay {self.employee_name}, here are the courses available in our database:\n"
                            for course in courses:
                                ai_response += f"- {course['name']}: {course['description']} Duration: {course['duration']}, Fees: INR {course['fees']}, Covers: {course['content']}\n"
                            ai_response += "Would you like to schedule a counseling session to discuss these, or pick one now? What else can I help you with?"
                            clean_response = clean_markdown(ai_response)
                            await self.generate_audio(clean_response, ai_response)
                            await save_interaction(self.employee_id, transcript, ai_response)
                            self.last_intent = "course_suggestion"
                            print(f"Intent: course_suggestion")
                            logging.info(f"Intent: course_suggestion")
                            return
                        else:
                            ai_response = "No courses available right now. Would you like to schedule a counseling session anyway? What else can I help you with?"
                            clean_response = clean_markdown(ai_response)
                            await self.generate_audio(clean_response, ai_response)
                            await save_interaction(self.employee_id, transcript, ai_response)
                            return

                # Handle mode selection
                if self.counseling_context == "awaiting_mode":
                    if "online" in transcript_lower:
                        self.counseling_mode = "online"
                        self.counseling_context = None
                        self.last_intent = "schedule"
                        ai_response = f"Okay {self.employee_name}, let's schedule your online counseling session for {self.selected_course}. When are you free, like May 15, 2025 at 11 AM?"
                        clean_response = clean_markdown(ai_response)
                        await self.generate_audio(clean_response, ai_response)
                        await save_interaction(self.employee_id, transcript, ai_response)
                        print(f"Intent: schedule (online, course: {self.selected_course})")
                        logging.info(f"Intent: schedule (online, course: {self.selected_course})")
                        return
                    elif "offline" in transcript_lower:
                        self.counseling_mode = "offline"
                        self.counseling_context = None
                        self.last_intent = "schedule"
                        ai_response = f"Okay {self.employee_name}, let's schedule your offline counseling session at our office for {self.selected_course}. When are you free, like May 15, 2025 at 11 AM?"
                        clean_response = clean_markdown(ai_response)
                        await self.generate_audio(clean_response, ai_response)
                        await save_interaction(self.employee_id, transcript, ai_response)
                        print(f"Intent: schedule (offline, course: {self.selected_course})")
                        logging.info(f"Intent: schedule (offline, course: {self.selected_course})")
                        return
                    else:
                        ai_response = "I didn't catch if you want online or offline counseling. Could you say 'online' or 'offline'?"
                        clean_response = clean_markdown(ai_response)
                        await self.generate_audio(clean_response, ai_response)
                        await save_interaction(self.employee_id, transcript, ai_response)
                        print(f"Intent: awaiting mode clarification")
                        logging.info(f"Intent: awaiting mode clarification")
                        return

                # Handle scheduling after course suggestion
                if self.counseling_context == "course_suggestion" and any(phrase in transcript_lower for phrase in ["schedule", "book", "counseling"]):
                    self.counseling_context = "awaiting_choice"
                    ai_response = f"Okay {self.employee_name}, do you have a course in mind, or would you like help choosing one?"
                    clean_response = clean_markdown(ai_response)
                    await self.generate_audio(clean_response, ai_response)
                    await save_interaction(self.employee_id, transcript, ai_response)
                    print(f"Intent: awaiting course choice (post course suggestion)")
                    logging.info(f"Intent: awaiting course choice (post course suggestion)")
                    return

                # Check for course inquiries
                course_keywords = ["course", "courses", "python", "java", "data science", "web development"]
                is_course_query = any(keyword in transcript_lower for keyword in course_keywords)
                
                if is_course_query:
                    course_name = detected_course or self.selected_course or next((keyword for keyword in course_keywords if keyword in transcript_lower), None)
                    if course_name:
                        course_details = await fetch_course_details(course_name)
                        if course_details:
                            self.selected_course = course_details['name']
                            self.counseling_context = "awaiting_mode"
                            ai_response = f"Okay {self.employee_name}, the {course_details['name']} course includes {course_details['description']} It lasts {course_details['duration']}, costs INR {course_details['fees']}, and covers: {course_details['content']}. Would you like this counseling session to be online or offline at our office?"
                            clean_response = clean_markdown(ai_response)
                            await self.generate_audio(clean_response, ai_response)
                            await save_interaction(self.employee_id, transcript, ai_response)
                            self.last_intent = "course"
                            print(f"Intent: course (course: {course_name}, awaiting mode)")
                            logging.info(f"Intent: course for {course_name}, awaiting mode")
                            return
                        else:
                            ai_response = f"Okay {self.employee_name}, I couldn't find {course_name}. We offer Python, Java, Data Science, and Web Development. Which one would you like to know about? What else can I help you with?"
                            clean_response = clean_markdown(ai_response)
                            await self.generate_audio(clean_response, ai_response)
                            await save_interaction(self.employee_id, transcript, ai_response)
                            self.last_intent = "course"
                            print(f"Intent: course (course: {course_name})")
                            logging.info(f"Intent: course for {course_name}")
                            return
                    else:
                        ai_response = f"Okay {self.employee_name}, I couldn't find that course. We offer Python, Java, Data Science, and Web Development. Which one would you like to know about? What else can I help you with?"
                        clean_response = clean_markdown(ai_response)
                        await self.generate_audio(clean_response, ai_response)
                        await save_interaction(self.employee_id, transcript, ai_response)
                        self.last_intent = "course"
                        print(f"Intent: course (no specific course)")
                        logging.info(f"Intent: course (no specific course)")
                        return

                response = await asyncio.to_thread(self.chat.send_message, context + transcript)
                ai_response = response.text.strip()
                clean_response = clean_markdown(ai_response)
                await self.generate_audio(clean_response + " What else can I help you with?", ai_response)
                await save_interaction(self.employee_id, transcript, ai_response)
                self.last_response = clean_response

                is_reschedule = any(phrase in transcript_lower for phrase in ["reschedule", "change my session", "move my session"])
                is_confirmation = any(phrase in transcript_lower for phrase in ["yes", "correct", "confirm", "all details are correct"])
                is_mode_selection = "online" in transcript_lower or "offline" in transcript_lower
                ai_indicates_reschedule = "rescheduled" in ai_response.lower()

                if is_reschedule:
                    self.last_intent = "reschedule"
                    self.counseling_context = "awaiting_mode"
                    ai_response = f"Okay {self.employee_name}, would you like your rescheduled counseling session for {self.selected_course or 'your course'} to be online or offline at our office?"
                    clean_response = clean_markdown(ai_response)
                    await self.generate_audio(clean_response, ai_response)
                    await save_interaction(self.employee_id, transcript, ai_response)
                    print(f"Intent: reschedule (awaiting mode)")
                    logging.info(f"Intent: reschedule (awaiting mode)")
                    return
                elif is_confirmation and (self.last_intent == "reschedule" or ai_indicates_reschedule):
                    self.last_intent = "reschedule"
                    mode = self.counseling_mode or "offline"
                    print(f"Intent: reschedule (confirmation: {transcript}, mode: {mode})")
                    logging.info(f"Intent: reschedule confirmation (mode: {mode})")
                    reschedule_response = await reschedule_session(self.employee_id, self.employee_name, self.employee_phone, self.sms_consent, ai_response, mode, self.selected_course)
                    await self.generate_audio(reschedule_response + " What else can I help you with?", reschedule_response)
                    await save_interaction(self.employee_id, transcript, reschedule_response)
                elif is_mode_selection and self.last_intent == "reschedule":
                    self.counseling_mode = "online" if "online" in transcript_lower else "offline"
                    self.counseling_context = None
                    ai_response = f"Okay {self.employee_name}, let's reschedule your {self.counseling_mode} counseling session for {self.selected_course or 'your course'}. When are you free, like May 15, 2025 at 11 AM?"
                    clean_response = clean_markdown(ai_response)
                    await self.generate_audio(clean_response, ai_response)
                    await save_interaction(self.employee_id, transcript, ai_response)
                    print(f"Intent: reschedule (mode: {self.counseling_mode})")
                    logging.info(f"Intent: reschedule (mode: {self.counseling_mode})")
                    return
                else:
                    self.last_intent = "schedule"
                    mode = self.counseling_mode or "offline"
                    print(f"Intent: schedule (default, transcript: {transcript}, mode: {mode})")
                    logging.info(f"Intent: schedule (mode: {mode})")
                    schedule_response = await save_session(self.employee_id, self.employee_name, self.employee_phone, self.sms_consent, ai_response, mode, self.selected_course)
                    await self.generate_audio(schedule_response + " What else can I help you with?", schedule_response)
                    await save_interaction(self.employee_id, transcript, schedule_response)
            else:
                await self.get_employee_details()
        except Exception as e:
            print(f"Error generating AI response: {e}")
            logging.error(f"Error generating AI response: {e}")
            await self.generate_audio("Sorry, I didn't catch that. Could you say it again? What else can I help you with?",
                                     "Sorry, I didn't catch that. Could you say it again?")

    async def generate_audio(self, clean_text, raw_text):
        """Generate and play audio for AI response, using cache."""
        print(f"\nHR Assistant: {raw_text}")
        logging.info(f"HR Assistant response: {raw_text}")
        try:
            if raw_text not in self.audio_cache:
                self.tts_engine.say(clean_text)
                self.audio_cache[raw_text] = True
            self.tts_engine.runAndWait()
        except Exception as e:
            print(f"Error generating or playing audio: {e}")
            logging.error(f"Error generating or playing audio: {e}")

async def main():
    greeting = "Hello, I'm Emma, your HR assistant at Regex Software. How can I assist you today?"
    try:
        await create_tables()
        hr_assistant = HR_Assistant()
        try:
            await hr_assistant.generate_audio(greeting, greeting)
            await hr_assistant.get_employee_details()
            await hr_assistant.start_transcription()
        finally:
            hr_assistant.cleanup()
    except KeyboardInterrupt:
        print("\nShutting down...")
        logging.info("Shutting down HR Assistant")
        hr_assistant.cleanup()
    except Exception as e:
        print(f"Error in main: {e}")
        logging.error(f"Error in main: {e}")

if __name__ == "__main__":
    asyncio.run(main())