from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import sqlite3
from twilio.rest import Client
import random
import stripe
import os
import requests

app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Change in prod

login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Env vars
TWILIO_SID = os.getenv('TWILIO_SID')
TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
TWILIO_PHONE = os.getenv('TWILIO_PHONE')
STRIPE_SECRET = os.getenv('STRIPE_SECRET_KEY')
GROK_API_KEY = os.getenv('GROK_API_KEY')
stripe.api_key = STRIPE_SECRET
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

# DB setup
conn = sqlite3.connect('disputes.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, phone TEXT UNIQUE, verified INTEGER)''')
c.execute('''CREATE TABLE IF NOT EXISTS disputes (id INTEGER PRIMARY KEY, creator_id INTEGER, status TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS parties (id INTEGER PRIMARY KEY, dispute_id INTEGER, user_id INTEGER, submitted INTEGER, truth TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS resolutions (id INTEGER PRIMARY KEY, dispute_id INTEGER, verdict TEXT)''')
conn.commit()

class User(UserMixin):
    def __init__(self, id, phone):
        self.id = id
        self.phone = phone

@login_manager.user_loader
def load_user(user_id):
    c.execute('SELECT * FROM users WHERE id=?', (user_id,))
    row = c.fetchone()
    if row:
        return User(row[0], row[1])
    return None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        phone = request.form['phone']
        c.execute('INSERT OR IGNORE INTO users (phone, verified) VALUES (?, 0)', (phone,))
        conn.commit()
        session['phone'] = phone
        send_verification(phone)
        return redirect(url_for('verify'))
    return render_template('signup.html')

def send_verification(phone):
    code = random.randint(100000, 999999)
    session['code'] = code
    twilio_client.messages.create(body=f'Your code: {code}', from_=TWILIO_PHONE, to=phone)

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if request.method == 'POST':
        code = int(request.form['code'])
        if code == session.get('code'):
            phone = session['phone']
            c.execute('UPDATE users SET verified=1 WHERE phone=?', (phone,))
            c.execute('SELECT id FROM users WHERE phone=?', (phone,))
            user_id = c.fetchone()[0]
            conn.commit()
            user = User(user_id, phone)
            login_user(user)
            flash('Verified')
            return redirect('/')
        flash('Invalid code')
    return render_template('verify.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        phone = request.form['phone']
        c.execute('SELECT * FROM users WHERE phone=? AND verified=1', (phone,))
        if c.fetchone():
            session['phone'] = phone
            send_verification(phone)
            return redirect(url_for('verify'))
        flash('Sign up first')
    return render_template('signup.html')  # Reuse for login

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/')

@app.route('/create_dispute', methods=['GET', 'POST'])
@login_required
def create_dispute():
    if request.method == 'POST':
        c.execute('INSERT INTO disputes (creator_id, status) VALUES (?, "open")', (current_user.id,))
        dispute_id = c.lastrowid
        conn.commit()
        # Add creator as party 1
        c.execute('INSERT INTO parties (dispute_id, user_id, submitted) VALUES (?, ?, 0)', (dispute_id, current_user.id,))
        # Assume 2 via link; for now redirect
        return redirect(url_for('dispute', dispute_id=dispute_id))
    return render_template('create_dispute.html')

@app.route('/dispute/<int:dispute_id>', methods=['GET', 'POST'])
@login_required
def dispute(dispute_id):
    # Join if not party
    c.execute('SELECT * FROM parties WHERE dispute_id=? AND user_id=?', (dispute_id, current_user.id))
    if not c.fetchone():
        c.execute('INSERT INTO parties (dispute_id, user_id, submitted) VALUES (?, ?, 0)', (dispute_id, current_user.id))
        conn.commit()
    # Show parties
    c.execute('SELECT users.phone, parties.submitted FROM parties JOIN users ON users.id = parties.user_id WHERE dispute_id=?', (dispute_id,))
    parties = c.fetchall()
    # If submitted
    if all submitted, generate if not
    c.execute('SELECT COUNT(*) FROM parties WHERE dispute_id=?', (dispute_id,))
    total = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM parties WHERE dispute_id=? AND submitted=1', (dispute_id,))
    subs = c.fetchone()[0]
    if subs == total and total > 1:
        generate_verdict(dispute_id)
    # Get verdict if exists
    c.execute('SELECT verdict FROM resolutions WHERE dispute_id=?', (dispute_id,))
    verdict = c.fetchone()
    return render_template('dispute.html', dispute_id=dispute_id, parties=parties, verdict=verdict, link=f'{request.host_url}dispute/join/{dispute_id)}')

@app.route('/dispute/join/<int:dispute_id>')
def join_dispute(dispute_id):
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    return redirect(url_for('dispute', dispute_id=dispute_id))

@app.route('/submit_truth/<int:dispute_id>', methods=['POST'])
@login_required
def submit_truth(dispute_id):
    truth = request.form['truth']
    # Pay $1
    try:
        charge = stripe.Charge.create(
            amount=100,  # cents
            currency='usd',
            description='Dispute submit',
            source=request.form['stripeToken']  # From frontend form
        )
        c.execute('UPDATE parties SET submitted=1, truth=? WHERE dispute_id=? AND dispute_id=?', (truth, current_user.id, dispute_id))
        conn.commit()
        flash('Submitted and paid')
    except:
        flash('Payment failed')
    return redirect(url_for('dispute', dispute_id=dispute_id))

def generate_verdict(dispute_id):
    c.execute('SELECT truth FROM parties WHERE dispute_id=?', (dispute_id,))
    truths = [row[0] for row in c.fetchall()]
    prompt = f"Resolve fairly: Party1: {truths[0]} Party2: {truths[1] if len(truths)>1 else ''}"  # Extend for more
    headers = {'Authorization': f'Bearer {GROK_API_KEY}', 'Content-Type': 'application/json'}
    data = {'model': 'grok', 'messages': [{'role': 'user', 'content': prompt}]}
    response = requests.post('https://api.x.ai/v1/chat/completions', headers=headers, json=data)  # Adjust endpoint per docs
    verdict = response.json()['choices'][0]['message']['content']
    c.execute('INSERT INTO resolutions (dispute_id, verdict) VALUES (?, ?)', (dispute_id, verdict))
    conn.commit()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
