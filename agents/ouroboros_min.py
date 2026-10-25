"""Ouroboros with the min profile; see ouroboros_convy/launch.py for what it sets."""

from ouroboros_convy import Ouroboros, Server

server = Server("min")
agent = Ouroboros(server)
version = server.version()
