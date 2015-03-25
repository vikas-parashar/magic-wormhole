
# this starts a relay server on port 8009 and a transit server on 8010
start:
	ve/bin/twistd -y src/wormhole/servers/relay.py

stop:
	kill `cat twistd.pid`

restart:
	$(MAKE) stop
	sleep 1
	$(MAKE) start

update:
	git pull
	$(MAKE) restart

ve:
	virtualenv ve
	ve/bin/pip install pynacl twisted
	ve/bin/python setup.py develop
