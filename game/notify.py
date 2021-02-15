import aiosqlite, asyncio

class Notify:
    def connection(self, name):
        return aiosqlite.connect(f'{name}.db')

    async def setup(self, notify_name, *, name):
        async with self.connection(notify_name) as conn:
            await conn.execute(f'CREATE TABLE if not exists {notify_name} (user int)')
            await conn.commit()

    async def toggle(self, user, *, lock, name, conn):
        async with lock:
            c = await conn.execute(f'SELECT user FROM {name} WHERE user=?', (user,))
            data = await c.fetchall()
            if data: await conn.execute(f'DELETE FROM {name} WHERE user=?', (user,))
            else: await conn.execute(f'INSERT INTO {name} VALUES (?)', (user,))
            await conn.commit()

        return await self.get(user, lock=lock, name=name, conn=conn)

    async def get(self, user, *, lock, name, conn):
        async with lock:
            c = await conn.execute(f'SELECT user FROM {name} WHERE user=?', (user,))
            data = await c.fetchall()
        return bool(data)

    async def get_all(self, *, lock, name, conn):
        async with lock:
            c = await conn.execute(f'SELECT * FROM {name}')
            data = await c.fetchall()
        return [x[0] for x in data]