"""Ouroboros with a scripted local model: checks the integration for free, not its quality."""

from ouroboros_convy import Ouroboros, Server

server = Server("min", stub=True)
agent = Ouroboros(server)
version = server.version()
