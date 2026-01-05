## (C) Navid Momtahen 2025 (GPL-3.0)

import std/[os, times, strutils, re, osproc, locks]
import db_connector/db_sqlite

type
  IndexerContext = object
    db: DbConn
    dbPath: cstring
    stopFlag: bool
    lock: Lock

var globalLock: Lock

initLock(globalLock)

proc NimMain() {.importc.}

{.push exportc, dynlib, cdecl.}

proc initNim*() =
  ## Initialize Nim runtime (call once before using any functions)
  NimMain()

proc createIndexer*(dbPath: cstring): pointer =
  ## Create a new indexer context
  acquire(globalLock)
  defer: release(globalLock)
  
  var ctx = cast[ptr IndexerContext](alloc0(sizeof(IndexerContext)))  
  let pathStr = $dbPath
  let pathLen = pathStr.len
  
  ctx.dbPath = cast[cstring](alloc0(pathLen + 1))
  copyMem(ctx.dbPath, cstring(pathStr), pathLen)
  
  ctx.stopFlag = false
  initLock(ctx.lock)
  
  let dir = parentDir(pathStr)
  if dir.len > 0 and not dirExists(dir):
    try:
      createDir(dir)
    except:
      dealloc(ctx.dbPath)
      dealloc(ctx)
      return nil
  
  try:
    ctx.db = open(pathStr, "", "", "")
   
    ctx.db.exec(sql"PRAGMA journal_mode=WAL")
    ctx.db.exec(sql"PRAGMA synchronous=NORMAL")
    ctx.db.exec(sql"PRAGMA cache_size=-64000")
    ctx.db.exec(sql"PRAGMA temp_store=MEMORY")
    ctx.db.exec(sql"PRAGMA mmap_size=268435456")  
    ctx.db.exec(sql"""
      CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        filename TEXT NOT NULL,
        extension TEXT,
        size INTEGER,
        modified INTEGER,
        is_directory INTEGER,
        filesystem_type TEXT,
        indexed_at INTEGER
      )
    """)
    
    ctx.db.exec(sql"CREATE INDEX IF NOT EXISTS idx_filename ON files(filename COLLATE NOCASE)")
    ctx.db.exec(sql"CREATE INDEX IF NOT EXISTS idx_path ON files(path COLLATE NOCASE)")
    ctx.db.exec(sql"CREATE INDEX IF NOT EXISTS idx_extension ON files(extension)")
    
    ctx.db.exec(sql"""
      CREATE TABLE IF NOT EXISTS mount_points (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT UNIQUE NOT NULL,
        filesystem_type TEXT,
        last_indexed INTEGER,
        enabled INTEGER DEFAULT 1
      )
    """)
  except:
    if not ctx.dbPath.isNil:
      dealloc(ctx.dbPath)
    dealloc(ctx)
    return nil
  
  return cast[pointer](ctx)

proc destroyIndexer*(ctx: pointer) =
  ## Destroy indexer context and close database
  if ctx.isNil:
    return
  let indexer = cast[ptr IndexerContext](ctx)
  try:
    indexer.db.close()
  except:
    discard
  if not indexer.dbPath.isNil:
    dealloc(indexer.dbPath)
  dealloc(indexer)

proc setStopFlag*(ctx: pointer, stop: bool) =
  ## Set stop flag for indexing
  if ctx.isNil:
    return
  let indexer = cast[ptr IndexerContext](ctx)
  acquire(indexer.lock)
  indexer.stopFlag = stop
  release(indexer.lock)

proc detectFilesystem*(path: cstring): cstring =
  ## Detect filesystem type for a path
  try:
    let (output, exitCode) = execCmdEx("df -T " & quoteShell($path))
    if exitCode == 0:
      let lines = output.splitLines()
      if lines.len >= 2:
        let parts = lines[1].splitWhitespace()
        if parts.len >= 2:
          let theResult = parts[1]
          let cstr = cast[cstring](alloc0(theResult.len + 1))
          copyMem(cstr, cstring(theResult), theResult.len)
          return cstr
  except:
    discard
  let unknown = "unknown"
  let cstr = cast[cstring](alloc0(unknown.len + 1))
  copyMem(cstr, cstring(unknown), unknown.len)
  return cstr

proc addMountPoint*(ctx: pointer, path: cstring, fsType: cstring): bool =
  ## Add a mount point to be indexed
  if ctx.isNil:
    return false
  let indexer = cast[ptr IndexerContext](ctx)
  acquire(indexer.lock)
  defer: release(indexer.lock)
  try:
    indexer.db.exec(sql"""
      INSERT OR REPLACE INTO mount_points (path, filesystem_type, enabled)
      VALUES (?, ?, 1)
    """, $path, $fsType)
    return true
  except:
    return false

proc indexPath*(ctx: pointer, rootPath: cstring, progressCallback: proc(count: int, path: cstring) {.cdecl.}): int =
  ## Index all files in a given path
  if ctx.isNil:
    return 0
  let indexer = cast[ptr IndexerContext](ctx)
  
  let root = $rootPath
  var indexedCount = 0
  let fsTypeCstr = detectFilesystem(rootPath)
  let fsType = $fsTypeCstr
  
  acquire(indexer.lock)
  try:
    indexer.db.exec(sql"DELETE FROM files WHERE path LIKE ?", root & "%")
  except:
    release(indexer.lock)
    return 0
  finally:
    release(indexer.lock)
  
  var batch: seq[tuple[path, filename, extension: string, size, modified: int64, 
                       isDir: int, fsType: string, indexedAt: int64]]
  const batchSize = 10000
  batch = newSeqOfCap[type(batch[0])](batchSize)
  
  let insertSql = sql"""
    INSERT INTO files (path, filename, extension, size, modified, 
                     is_directory, filesystem_type, indexed_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
  """
  
  try:
    acquire(indexer.lock)
    try:
      indexer.db.exec(sql"DROP INDEX IF EXISTS idx_filename")
      indexer.db.exec(sql"DROP INDEX IF EXISTS idx_path")
      indexer.db.exec(sql"DROP INDEX IF EXISTS idx_extension")
    except:
      discard
    
    indexer.db.exec(sql"PRAGMA synchronous=OFF")
    indexer.db.exec(sql"BEGIN TRANSACTION")
    release(indexer.lock)
    
    var fileCounter = 0  
    const stopCheckInterval = 500      
    let batchIndexedAt = getTime().toUnix()
    
    try:
      for entry in walkDirRec(root, {pcFile, pcDir}, {pcDir}, relative = false, checkDir = false):
        inc fileCounter
        if fileCounter mod stopCheckInterval == 0:
          acquire(indexer.lock)
          let shouldStop = indexer.stopFlag
          release(indexer.lock)
          if shouldStop:
            break
        
        try:
          let info = getFileInfo(entry)
          let filename = extractFilename(entry)
          let ext = splitFile(entry).ext.toLowerAscii()
          let isDir = if info.kind == pcDir: 1 else: 0
          let size = if info.kind == pcFile: info.size else: 0
          let modified = info.lastWriteTime.toUnix()
          
          batch.add((entry, filename, ext, size, modified, isDir, fsType, batchIndexedAt))
          
          if isDir == 0:
            inc indexedCount
          
          if batch.len >= batchSize:
            acquire(indexer.lock)
            for item in batch:
              indexer.db.exec(insertSql, item.path, item.filename, item.extension, 
                            item.size, item.modified, item.isDir, item.fsType, item.indexedAt)
            
            indexer.db.exec(sql"COMMIT")
            indexer.db.exec(sql"BEGIN TRANSACTION")
            release(indexer.lock)
            
            if not progressCallback.isNil:
              let pathCopy = cast[cstring](alloc0(entry.len + 1))
              copyMem(pathCopy, cstring(entry), entry.len)
              progressCallback(indexedCount, pathCopy)
              dealloc(pathCopy)
            
            batch.setLen(0)
        
        except OSError, IOError:
          continue
    
    except OSError, IOError:
      discard
    
    acquire(indexer.lock)
    if batch.len > 0:
      for item in batch:
        indexer.db.exec(insertSql, item.path, item.filename, item.extension, 
                       item.size, item.modified, item.isDir, item.fsType, item.indexedAt)
    
    indexer.db.exec(sql"COMMIT")
    indexer.db.exec(sql"CREATE INDEX IF NOT EXISTS idx_filename ON files(filename COLLATE NOCASE)")
    indexer.db.exec(sql"CREATE INDEX IF NOT EXISTS idx_path ON files(path COLLATE NOCASE)")
    indexer.db.exec(sql"CREATE INDEX IF NOT EXISTS idx_extension ON files(extension)")
    indexer.db.exec(sql"PRAGMA synchronous=NORMAL")
    indexer.db.exec(sql"""
      UPDATE mount_points SET last_indexed = ? WHERE path = ?
    """, getTime().toUnix(), root)
    
    release(indexer.lock)
    
  except:
    acquire(indexer.lock)
    try:
      indexer.db.exec(sql"ROLLBACK")
      indexer.db.exec(sql"PRAGMA synchronous=NORMAL")
      try:
        indexer.db.exec(sql"CREATE INDEX IF NOT EXISTS idx_filename ON files(filename COLLATE NOCASE)")
        indexer.db.exec(sql"CREATE INDEX IF NOT EXISTS idx_path ON files(path COLLATE NOCASE)")
        indexer.db.exec(sql"CREATE INDEX IF NOT EXISTS idx_extension ON files(extension)")
      except:
        discard
    except:
      discard
    release(indexer.lock)
    return 0
  
  return indexedCount

proc search*(ctx: pointer, query: cstring, matchCase: bool, regexMode: bool,
             searchPath: bool, fileType: cstring, maxResults: int,
             results: ptr ptr cstring, resultCount: ptr int): bool =
  ## Search for files matching query
  if ctx.isNil or results.isNil or resultCount.isNil:
    if not resultCount.isNil:
      resultCount[] = 0
    return false
  
  results[] = nil
  resultCount[] = 0
  
  let indexer = cast[ptr IndexerContext](ctx)
  let q = $query
  let fType = $fileType
  
  if q.len == 0:
    return true
  
  var rows: seq[Row] = @[]
  
  acquire(indexer.lock)
  try:
    if regexMode:
      let allRows = indexer.db.getAllRows(sql"""
        SELECT path, filename, size, modified, is_directory, filesystem_type
        FROM files
        ORDER BY is_directory DESC, filename
        LIMIT ?
      """, maxResults * 10)
      
      let flags = if matchCase: {reStudy} else: {reIgnoreCase, reStudy}
      let pattern = re(q, flags)
      
      for row in allRows:
        let searchText = if searchPath: row[0] else: row[1]
        if searchText.contains(pattern):
          rows.add(row)
          if rows.len >= maxResults:
            break
    else:
      let likeQuery = "%" & q & "%"
      let collate = if matchCase: "" else: "COLLATE NOCASE"
      
      var whereClause = if searchPath:
        "path LIKE ? " & collate
      else:
        "filename LIKE ? " & collate
      
      case fType
      of "files":
        whereClause &= " AND is_directory = 0"
      of "folders":
        whereClause &= " AND is_directory = 1"
      else:
        discard
      
      let queryStr = "SELECT path, filename, size, modified, is_directory, filesystem_type FROM files WHERE " & 
                      whereClause & " ORDER BY is_directory DESC, filename LIMIT ?"
      
      rows = indexer.db.getAllRows(sql(queryStr), likeQuery, maxResults)
    
    release(indexer.lock)
    
    resultCount[] = rows.len
    if rows.len > 0:
      var resultArray = cast[ptr UncheckedArray[cstring]](alloc0(rows.len * sizeof(cstring)))
      
      for i, row in rows:
        let jsonStr = row[0] & "|" & row[1] & "|" & row[2] & "|" & row[3] & "|" & row[4] & "|" & row[5]
        let cstr = cast[cstring](alloc0(jsonStr.len + 1))
        copyMem(cstr, cstring(jsonStr), jsonStr.len)
        resultArray[i] = cstr
      
      results[] = cast[ptr cstring](resultArray)
    
    return true
  except:
    release(indexer.lock)
    resultCount[] = 0
    results[] = nil
    return false

proc freeSearchResults*(results: ptr cstring, count: int) =
  ## Free memory allocated for search results
  if not results.isNil:
    let arr = cast[ptr UncheckedArray[cstring]](results)
    for i in 0..<count:
      if not arr[i].isNil:
        dealloc(arr[i])
    dealloc(results)

proc getStats*(ctx: pointer, fileCount: ptr int64, dirCount: ptr int64, totalSize: ptr int64): bool =
  ## Get database statistics
  if ctx.isNil:
    return false
  let indexer = cast[ptr IndexerContext](ctx)
  
  acquire(indexer.lock)
  defer: release(indexer.lock)
  
  try:
    let fileRow = indexer.db.getRow(sql"SELECT COUNT(*) FROM files WHERE is_directory = 0")
    fileCount[] = parseInt(fileRow[0])
    
    let dirRow = indexer.db.getRow(sql"SELECT COUNT(*) FROM files WHERE is_directory = 1")
    dirCount[] = parseInt(dirRow[0])
    
    let sizeRow = indexer.db.getRow(sql"SELECT COALESCE(SUM(size), 0) FROM files WHERE is_directory = 0")
    totalSize[] = if sizeRow[0].len > 0 and sizeRow[0] != "": parseInt(sizeRow[0]) else: 0
    
    return true
  except:
    fileCount[] = 0
    dirCount[] = 0
    totalSize[] = 0
    return false

proc getIndexedMountPoints*(ctx: pointer, paths: ptr ptr cstring, fsTypes: ptr ptr cstring,
                            lastIndexed: ptr ptr int64, enabled: ptr ptr int,
                            count: ptr int): bool =
  ## Get indexed mount points
  if ctx.isNil:
    count[] = 0
    return false
  let indexer = cast[ptr IndexerContext](ctx)
  
  acquire(indexer.lock)
  defer: release(indexer.lock)
  
  try:
    let rows = indexer.db.getAllRows(sql"""
      SELECT path, filesystem_type, COALESCE(last_indexed, 0), enabled
      FROM mount_points
    """)
    
    count[] = rows.len
    if rows.len > 0:
      var pathArray = cast[ptr UncheckedArray[cstring]](alloc0(rows.len * sizeof(cstring)))
      var fsArray = cast[ptr UncheckedArray[cstring]](alloc0(rows.len * sizeof(cstring)))
      var timeArray = cast[ptr UncheckedArray[int64]](alloc0(rows.len * sizeof(int64)))
      var enabledArray = cast[ptr UncheckedArray[int]](alloc0(rows.len * sizeof(int)))
      
      for i, row in rows:
        let pathStr = cast[cstring](alloc0(row[0].len + 1))
        copyMem(pathStr, cstring(row[0]), row[0].len)
        pathArray[i] = pathStr
        
        let fsStr = cast[cstring](alloc0(row[1].len + 1))
        copyMem(fsStr, cstring(row[1]), row[1].len)
        fsArray[i] = fsStr
        
        timeArray[i] = if row[2].len > 0 and row[2] != "": parseInt(row[2]) else: 0
        enabledArray[i] = parseInt(row[3])
      
      paths[] = cast[ptr cstring](pathArray)
      fsTypes[] = cast[ptr cstring](fsArray)
      lastIndexed[] = cast[ptr int64](timeArray)
      enabled[] = cast[ptr int](enabledArray)
    
    return true
  except:
    count[] = 0
    return false

proc freeMountPoints*(paths: ptr cstring, fsTypes: ptr cstring, lastIndexed: ptr int64,
                      enabled: ptr int, count: int) =
  ## Free memory allocated for mount points
  if not paths.isNil:
    let pathArr = cast[ptr UncheckedArray[cstring]](paths)
    for i in 0..<count:
      if not pathArr[i].isNil:
        dealloc(pathArr[i])
    dealloc(paths)
  
  if not fsTypes.isNil:
    let fsArr = cast[ptr UncheckedArray[cstring]](fsTypes)
    for i in 0..<count:
      if not fsArr[i].isNil:
        dealloc(fsArr[i])
    dealloc(fsTypes)
  
  if not lastIndexed.isNil:
    dealloc(lastIndexed)
  
  if not enabled.isNil:
    dealloc(enabled)

{.pop.}
