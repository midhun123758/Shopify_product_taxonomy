import json
from channels.generic.websocket import AsyncWebsocketConsumer

class TaxonomyUpdateConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.group_name = 'family_updates'

        # Join the broadcast group
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        # Leave the broadcast group
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name
        )

    # Receive message from room group
    async def family_update(self, event):
        # Send message to WebSocket
        await self.send(text_data=json.dumps({
            'type': 'family_update',
            'id': event.get('id'),
            'status': event.get('status'),
            'confidence_score': event.get('confidence_score'),
            'predicted_category': event.get('predicted_category'),
            'alternative_suggestions': event.get('alternative_suggestions')
        }))
