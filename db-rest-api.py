import json
import logging
import sqlite3
from flask import Flask, request, jsonify

app = Flask(__name__)

# Specify the full path to the SQLite database file
db_file_path = '/opt/gate-controller/data/gate-controller-database.db'

# Initialize the database connection
def initialize_db():
    conn = sqlite3.connect(db_file_path)
    cursor = conn.cursor()

    # Create the 'log' table if it doesn't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS log (
            id INTEGER PRIMARY KEY,
            timestamp DATETIME,
            image_path TEXT,
            plate_recognized TEXT,
            score REAL,
            fuzzy_match TEXT,
            gate_opened TEXT
        )
    ''')

    conn.commit()
    conn.close()

initialize_db()

# Define API endpoints for CRUD operations

@app.route('/log', methods=['GET'])
def get_log_entries():
    try:
        conn = sqlite3.connect(db_file_path)
        cursor = conn.cursor()

        # Retrieve all log entries
        cursor.execute('SELECT * FROM log')
        rows = cursor.fetchall()

        # Convert the log entries to a list of dictionaries
        log_entries = []
        for row in rows:
            entry = {
                'id': row[0],
                'timestamp': row[1],
                'image_path': row[2],
                'plate_recognized': row[3],
                'score': row[4],
                'fuzzy_match': row[5],
                'gate_opened': row[6]
            }
            log_entries.append(entry)

        conn.close()
        return jsonify({'log_entries': log_entries})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/log/<int:log_id>', methods=['GET'])
def get_log_entry(log_id):
    try:
        conn = sqlite3.connect(db_file_path)
        cursor = conn.cursor()

        # Retrieve a specific log entry by ID
        cursor.execute('SELECT * FROM log WHERE id = ?', (log_id,))
        row = cursor.fetchone()

        if row is None:
            conn.close()
            return jsonify({'message': 'Log entry not found'}), 404

        # Convert the log entry to a dictionary
        log_entry = {
            'id': row[0],
            'timestamp': row[1],
            'image_path': row[2],
            'plate_recognized': row[3],
            'score': row[4],
            'fuzzy_match': row[5],
            'gate_opened': row[6]
        }

        conn.close()
        return jsonify({'log_entry': log_entry})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Run the Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)