
const luaparse = require('luaparse');
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
try { luaparse.parse(src, { luaVersion: 'LuaJIT' }); console.log('OK'); }
catch (e) { console.log('ERR: ' + e.message); }
