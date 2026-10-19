import websockets

clients = set()

def handler(websocket):
    clients.add(websocket)
    try:
        for message in websocket:
            websockets.broadcast(clients - {websocket}, message)
    finally:
        clients.discard(websocket)

with websockets.serve(handler, "0.0.0.0", 5000):
    print("Chat server running on ws://0.0.0.0:5000")
