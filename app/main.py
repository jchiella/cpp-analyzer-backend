from flask import Flask, abort, send_file, send_from_directory # the web framework we are using for handling incoming requests
import requests # the library used for nice and simple HTTP requests (for communicating with SendOwl's API)
import psycopg2 # the library used for interaction with PostgreSQL for storing license keys

from os import environ # used to acess environment variables on the server side
from time import sleep # for waiting between attempts to request

TIMEOUT = 5 # The amount of time in seconds to wait for SendOwl to return from our request
TRY_INTERVAL = 1 # The amount of time in seconds between tries to request from SendOwl
TRY_COUNT = 10 # The number of request tries to SendOwl before giving up

app = Flask(__name__) # create new Flask web app with the name of this file

@app.route("/") # if a request is sent to the root directory (just the bare url)
def home(): # just disply a simple Welcome message to test that the server is up
    return "<h1>Welcome to the CPP Analyzer Payment System Backend!!!</h1>"

@app.route("/installers/<path:path>")
def serve_installer(path):
    return send_from_directory('../installers', path)

@app.route("/updates.xml")
def serve_updates():
    return send_file('../updates.xml')

@app.route("/verify/<key>/<hid>") # if a request is sent to our url/verify/the key to verify, process it here
def verify_key(key, hid): # key is the key seeking verification, hid is the unique hardware identifier of the computer
    connection, cursor = connect_to_db() # create a connection the our database
                                         # connection is a link to the database for opening, closing and commiting changes
                                         # cursor is used to access the data inside the database
    if license_is_stored(key, cursor): # if the license exists in our database already
        stored_hid = get_stored_hid(key, cursor) # get the HID stored in the database for that key
        if hid == stored_hid: # the HID in the database must match the one we are sent (otherwise the license key is being used on multiple devices, which is not allowed)
            code_to_return = request_sendowl(key, hid) # send a request to SendOwl to verify that the key itself is valid if the HID matches
            if code_to_return == requests.codes.ok: # if SendOwl says that the key is valid
                update_valid_request_count(key, cursor) # update the valid request count in the database
                end_db_session(cursor, connection) # end the database access
                return 'OK' # return a dummy value and a 200 response back to the software
            else: # otherise if SendOwl says the key is invalid or there is an issue
                update_invalid_request_count(key, cursor) # update the valid request count in the database
                end_db_session(cursor, connection) # end the database access
                abort(code_to_return) # and return the error from SendOwl
        else: # otherwise, if the HID does not match, no need to even bother checking with SendOwl
            update_invalid_request_count(key, cursor) # update the valid request count in the database
            end_db_session(cursor, connection) # so just end the DB access
            abort(requests.codes.not_found) # and tell the software the code is invalid
    else: # otherwise if the license key does not exist in our database, try to create it
        code_to_return = request_sendowl(key, hid) # check with SendOwl that it is valid
        if code_to_return == requests.codes.ok: # and if it is, 
            add_license_to_db(key, hid, cursor) # add it to our database with the HID
            end_db_session(cursor, connection) # and end the DB session
            return 'OK' # return a dummy OK response to the software
        else: # if it's not valid according to SendOwl
            end_db_session(cursor, connection) # end the DB session
            abort(code_to_return) # return the error from SendOwl


def connect_to_db(): # connects to the PostgreSQL database
    DATABASE_URL = environ['DATABASE_URL'] # this environment variable is created by Heroku and stores the link to our database
                                           # Heroku could change this at any time so we need to get it on every connection
    connection = psycopg2.connect(DATABASE_URL, sslmode='require') # connect to the database
                                                                   # Heroku requires that sslmode is set to require
    return connection, connection.cursor() # return the connection and a cursor for that connection


def request_sendowl(key, hid): # send a request to SendOwl to verify the key
    API_KEY = environ['API_KEY'] # The API key from SendOwl (created from SendOwl account with Manager permissions)
    API_SECRET = environ['API_SECRET'] # The API secret from SendOwl
    PRODUCT_ID = environ['PRODUCT_ID'] # The ID of the product to query

    BASE_API_PATH = f'https://www.sendowl.com/api/v1/products/{PRODUCT_ID}/' # this is the path to the correct product to query
    parameters = {'key': key} # the HTTP parameters containing `key=the key we're querying`
    headers = {'Accept': 'application/json'} # the HTTP header, because the response must be in JSON format (this header is required by SendOwl)
    for _ in range(TRY_COUNT): # attempt the connection 10 times 
        try:
            # HTTP GET request to the base path/licenses/check_valid, passing the parameters and header
            # The `auth` is a tuple of the API key and the secret
            # Also ensure the request times out after TIMEOUT seconds 
            request = requests.get(f'{BASE_API_PATH}/licenses/check_valid', params=parameters, headers=headers, auth=(API_KEY, API_SECRET), timeout=TIMEOUT)
        except requests.exceptions.Timeout: # If the request times out, this exception will be raised, so
            sleep(TRY_INTERVAL) # wait a bit to avoid overwhelming the server with requests
            continue # and then continue the loop so we try again
        if request.status_code == requests.codes.ok: # if the request returns the HTTP OK response
            results = request.json() # parse the data into a JSON object containing info on the license key
            return handle_results(results, key, hid) # handle the results of the query
        elif request.status_code == requests.codes.timeout: # if the request timed out on the server end
            sleep(TRY_INTERVAL) # wait a bit to avoid overwhelming the server
            continue  # and continue the loop to try again
        else: # if the request code is neither OK nor a timeout, simply tell the software that there was an internal server error
            return requests.codes.server_error
    return request.codes.server_error # if we made it through the loop TRY_COUNT times without success, tell the software there was an server error


def handle_results(results, key, hid): # handle the results from SendOwl
    if len(results) == 0: # if SendOwl returns the empty list, this means there are no valid license keys matching the request, so
        return requests.codes.not_found # tell the software the key was not found
    elif results[0]['license']['order_refunded']: # if the license key has already been refunded, it is invalid, so
        return requests.codes.not_found # abort the request and return HTTP Not Found back to the software
    elif not results[0]['license']['order_id']: # if there is no order_id attached, the license key has been revoked and is invalid, so
        return requests.codes.not_found # abort the request and return HTTP Not Found back to the software
    else: # if the license key has not been refunded or revoked, it is valid
        return requests.codes.ok # so return a dummy response and HTTP OK to the software


def get_stored_hid(key, cursor): # retrieve the stored HID from the database for a specific license key
    cursor.execute(f"SELECT DeviceID FROM Licenses WHERE License='{key}';") # SQL query to select the Device ID matching the given license key
    device_id, = cursor.fetchone() # get the device ID from the cursor (the comma is because the result is a 1-tuple)
    return device_id # return the device ID


def license_is_stored(key, cursor): # whether or not the given key exists in the license database
    cursor.execute(f"SELECT * FROM Licenses WHERE License='{key}';") # select all licenses matching the given key
    return len(cursor.fetchall()) > 0 # return whether a license matches the key or not


def update_valid_request_count(key, cursor): # increment the valid request count field of the given license key
    cursor.execute(f"SELECT ValidRequestCount FROM Licenses WHERE License='{key}';") # retrive the current valid request count
    request_count, = cursor.fetchone()
    new_request_count = request_count + 1 # increment it by one
    cursor.execute(f"UPDATE Licenses SET ValidRequestCount = {new_request_count} WHERE License='{key}';") # set the new request count


def update_invalid_request_count(key, cursor): # increment the invalid request count field of the given license key
    cursor.execute(f"SELECT InvalidRequestCount FROM Licenses WHERE License='{key}';") # retrive the current invalid request count
    request_count, = cursor.fetchone()
    new_request_count = request_count + 1 # increment it by one
    cursor.execute(f"UPDATE Licenses SET InvalidRequestCount = {new_request_count} WHERE License='{key}';") # set the new request count


def add_license_to_db(key, hid, cursor): # add the license and hardware key to the database with a timestamp and request count of one
    # 'key' is the license key
    # 'hid' is the hardware identifier
    # 'now' is the current date and time in UTC standard time
    # 1 is the number of valid requests made for this license key
    # 0 is the number of invalid requests made for this license key
    cursor.execute(f"INSERT INTO Licenses VALUES ('{key}', '{hid}', timestamp with time zone 'now', 1, 0);") # insert the new row of the table


def end_db_session(cursor, connection): # commit all changes and close the database connection
    connection.commit() # commit changes
    cursor.close() # close the cursor
    connection.close() # close the DB connection