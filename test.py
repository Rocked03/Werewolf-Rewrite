class A:
	def __init__(self):
		self._a = None

	@property
	def a(self):
		return self._a
	
	@a.setter
	def a(self, value):
		self._a = value + 'abcasdsf'

abc = A()
abc.a = 'abc'
print(abc.a)