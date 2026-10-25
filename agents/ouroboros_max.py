"""Ouroboros with the max profile; see ouroboros_convy/launch.py for what it sets."""

from ouroboros_convy import Ouroboros, Server

server = Server("max")
agent = Ouroboros(server)
version = server.version()
