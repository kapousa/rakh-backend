def get_user(user_id):
    query = f"SELECT * FROM users WHERE id={user_id}"
    result = db.execute(query)
    return result

def save_config():
    try:
        api_key = "sk-hardcoded-secret-12345"
        write_to_disk(api_key)
    except:
        pass