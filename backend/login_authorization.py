import creditials as cr
from fyers_apiv3 import fyersModel


print(100)
# Create a session model with the provided credentials
session = fyersModel.SessionModel(
    client_id=cr.client_id,
    secret_key=cr.secret_key,
    redirect_uri=cr.redirect_uri,
    response_type=cr.response_type,
    grant_type=cr.grant_type
)

# Generate the auth code using the session model
response = session.generate_authcode()



# Print the auth code received in the response
print(response)


uri = input("Enter the URI: ")

# Split based on auth_code= and take second item, then split again on & and take first item
auth_code = uri.split('auth_code=')[1].split('&')[0]

print(f"Auth Code: {auth_code}")


session.set_token(auth_code)

# Generate the access token using the authorization code
response = session.generate_token()

# Print the response, which should contain the access token and other details
print(response)

access_token = response['access_token']
print(access_token)



# Save access token to file (overwrites if exists)
with open("access_token.txt", "w") as f:
    f.write(access_token)

print("✅ Access token saved to access_token.txt")