
# make a virtualenv for everything first

run-relay:
	twistd -noy src/wormhole/relay.py
