import aiosqlite, asyncio

class Stasis:
    def connection(self, name):
        return aiosqlite.connect(f'{name}.db')

    async def setup(self, stasis_name, *, name):
        async with self.connection(stasis_name) as conn:
            await conn.execute(f'CREATE TABLE if not exists {stasis_name} (user int, count int)')
            await conn.commit()

    async def update(self, user, count, *, setcount=False, lock, name, conn):
        async with lock:
            c = await conn.execute(f'SELECT DISTINCT user FROM {name}')
            if user not in [x for y in await c.fetchall() for x in y]:
                if count > 0 or (count == -1 and setcount):
                    await conn.execute(f'INSERT INTO {name} VALUES (?, ?)', (user, count))
            else:
                if not setcount:
                    c = await conn.execute(f'SELECT count FROM {name} WHERE user=?', (user,))
                    oldcount = (await c.fetchall())[0][0]
                    if oldcount != -1: count = count + oldcount 
                    else: 
                        count = -1
                        setcount = True
                if count > 0 or (count == -1 and setcount):
                    await conn.execute(f'UPDATE {name} SET count=? WHERE user=?', (count, user))
                else:
                    await conn.execute(f'DELETE FROM {name} WHERE user=?', (user,))
            await conn.commit()

        return await self.get(user, lock=lock, name=name, conn=conn)

    async def get(self, user, *, lock, name, conn):
        async with lock:
            c = await conn.execute(f'SELECT count FROM {name} WHERE user=?', (user,))
            data = await c.fetchall()
            if data: return data[0][0]
            else: return 0

    async def get_all_dict(self, *, lock, name, conn):
        async with lock:
            c = await conn.execute(f'SELECT * FROM {name}')
            data = await c.fetchall()
            return {d[0]: d[1] for d in data}
