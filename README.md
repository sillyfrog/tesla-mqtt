# tesla-mqtt

Tesla Car API to MQTT bridge for controlling car charging. I'm publishing for reference by others, it's not currently ready for prime-time.

This is based on [TeslaPy](https://github.com/tdorssers/TeslaPy). Due to changes in the Tesla auth process, getting the initial login is a bit of a mess.

Once downloaded, install the requirements (I would recommend a virtual env):

```
pip install -r requirements.txt
```

The next thing is to get a `cache.json` file, this is the messy bit. Download the latest [TeslaPy](https://github.com/tdorssers/TeslaPy), go to the directory, and then run ` python cli.py -e <your email>`. This will then open a web browser, and ask you to login. Once logged in, you should get a "Page Not Found" error page - ignore that - copy the full URL, and paste it into the prompt. The program will then exit, but you'll have a `cache.json` file in the current directory. Copy this file to your `teslacartomqtt` directory, you're then ready to run the program.

Start the program as follows:

```
python teslacartomqtt.py --mqtthost=<mqtthost> --email=<tesla login email>
```

You will then start getting events on your MQTT server, you can then set charge level, eg:

```
mosquitto_pub -t tesla/car/charge-limit/set -m 80
```
