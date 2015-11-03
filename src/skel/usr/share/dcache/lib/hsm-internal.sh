#!/bin/sh
#
# $Id: hsmcpV4.sh,v 1.1 2006-05-31 14:08:15 tigran Exp $
#
#set -x
#
#
#    dCache configuration
#
#   hsm set dcache -command=<fullPathToThisScript> # e.g., /usr/share/dcache/lib/hsm-internal.sh
#   hsm set dcache -mongoUrl=<urlOfMongoDb> # e.g., server.example.org/database
#
#########################################################
#
#   prerequisits
#
LOG=/var/log/dcache/hsm-internal.log
DEVTTY=$LOG
AWK=gawk
#
#
#########################################################
#
#  some help functions
#
usage() {
   echo "Usage : put|get <pnfsId> <filePath> [-si=<storageInfo>] [-key[=value] ...]" | tee -a $LOG >&2
}
report() {
   echo "`date +"%D-%T"` ($$) $pnfsid $1" | tee -a $LOG >&2
}
problem() {
   echo "($$) $2 ($1)" | tee -a $LOG >&2
   exit $1
}
errorReport() {
   echo "($$) $1" | tee -a ${LOG} >&2
   return 0
}
#
#
#########################################################
#
#  Resolve pnfsID into canonical file name.
#  Argument : pnfsID
#  Returns  : filename
#
resolvePnfsID() {
   mongo ${mongoUrl} --eval "print(db.files.findOne( { pnfsid: "${1}"} ).canonicalPath)"
   return $?
}
#
#########################################################
#
getStorageInfoKey() {

   echo $si | 
   $AWK -v key=$1 -F';' '{
   for(i=1;i<=NF;i++){
      split($i,a,"=") ;
      if(a[1]==key)print a[2]
      }
   }'
}
#
#     end of init
#
#########################################################
#
datasetPut() {
   ystore=${1}
   ygroup=${2}
   ybfid=${3}

   params="var id='${ybfid}'; var ystore='${ystore}'; var ygroup='${ygroup}';"
   reply=$(mongo --quiet ${mongoUrl} --eval "${params}" /usr/share/dcache/lib/datasetPut.js)
   result=$?

   [ ${result} -ne 0 ] && problem 102 "Error running mongo script"

   [ -z ${reply} ] && problem 2 "Not yet ready"

   iserror=$(expr "${reply}" : "ERROR \([0-9]*\).*")

   if [ $? -eq 0 ]
   then
      return ${iserror}
   else 
      echo "${reply}"
      return 0
   fi
}
#
###############################################################
###############################################################
###############################################################
#
#   The big main
#
###############################################################
#
#
#  say hallo to people
#
# report "$*"
#
###############################################################
#
#   have we been called correctly ?
#
if [ $# -lt 3 ] ; then 
   usage
   exit 4
fi
#
#
##################################################################################
#
# split the arguments into the options -<key>=<value> and the 
# positional arguments.
#
# report "Splitting arguments"
si=$(echo "$*"|grep -o -e '-si=.*;')
export si
dcapDoor=$(echo "$*"|grep -o -e '-dcapDoor=[^ $]*'|grep -o -e '[^=]*$')
export dcapDoor
mongoUrl=$(echo "$*"|grep -o -e '-mongoUrl=[^ $]*'|grep -o -e '[^=]*$')
export mongoUrl
dcapLib=$(echo "$*"|grep -o -e '-dcapLib=[^ $]*'|grep -o -e '[^=]*$')
export dcapLib
uri=$(echo "$*"|grep -o -e '-uri=[^ $]*'|grep -o -e '[^-][^u][^r][^i][^=].*$')
export uri
#
##################################################################################
#
# assign the manditory arguments
#
command="${1}"
pnfsid="${2}"
filename="${3}"
#
###############################################################
#
# check for some basic variables
#
# 
[ -z "${dcapLib}" ] && problem 3 "Variable 'dcapLib' not defined"
#
[ -z "${dcapDoor}" ] && problem 3 "Variable 'dcapDoor' not defined"
# report "Checking mongoUrl"
[ -z "${mongoUrl}" ] && problem 3 "Variable 'mongoUrl' not defined"
#
# make sure the storage info variables are available
# (Will be fetched with getStorageInfoKey)
#
# report "Checking SI"
#
[ -z "${si}" ] && problem 1 "StorageInfo (-si=...) not available" 
#
#########################################################
#
#   osm specific variables
#
store=`getStorageInfoKey store 2>/dev/null`
group=`getStorageInfoKey group 2>/dev/null`
bfid=`getStorageInfoKey bfid 2>/dev/null`
#
###############################################################
#
#      simulate mount time
#
# report "Simulating mount time"
#
if [ -z "${waitTime}" ] ; then waitTime=0 ; fi
#
if [ $waitTime != 0 ]
then
   # report "Waiting ${waitTime} seconds"
   sleep ${waitTime}
   # report "Returning from waiting ${waitTime} seconds"
fi
#
###############################################################
#
if [ $command = "get" ] ; then
   #
   #  splitting URI into pieces
   #
   # report "Splitting URI into pieces"
   #
   getStore=`expr "${uri}" : ".*/?store=\(.*\)&group.*"`
   getGroup=`expr "${uri}" : ".*group=\(.*\)&bfid.*"`
   getBfid=`expr "${uri}" : ".*bfid=\(.*\)"`
   #
   report "URI : ${uri}"
   report "Store=${getStore}; Group=${getGroup}; Bfid=${getBfid}"
   #
   [ \( -z "${getStore}" \) -o \( -z "${getGroup}" \) -o \( -z "${getBfid}" \) ] && \
      problem 22 "couldn't get sufficient info for 'copy' : store=>${getStore}< group=>${getGroup}< bfid=>${getBfid}<" 
   #
   # Is this an archive ?
   #
   archiveId=`expr "${getBfid}" : ".*:\(.*\)" 2>/dev/null`
   if [ $? -ne 0 ] ; then
      problem 243 "This is not an small files archive : ${getBfid}"
   fi
   #
   originalId=`expr "${getBfid}" : "\(.*\):.*" 2>/dev/null`
   report "Data File Pnfs ID : ${originalId}"
   #
   # handle 0-byte files
   #
   if [ "${archiveId}" = "*" ] ; then
     report "Restoring 0-byte file: ${originalId}."
     exit 0
   elif [ "${originalId}" = "*" ] ; then
     report "Restoring old faulty entry for 0-byte file. To fix set bfid to ${originalId}:${archiveId}"
     exit 0
   fi
   #
   extractDir=`dirname "${filename}"`
   #
   report "Extracting file into $extractDir"
   #
   cd "${extractDir}"
   report "Preloading ${dcapLib}"
   export LD_PRELOAD="${dcapLib}"
   unzip "pnfs://${dcapDoor}/${archiveId}" "${originalId}" 2>>$LOG
   rc=$?
   cd -
   if [ $rc -ne 0 ] ; then problem 243 "Unzip couldn't replay the file($rc). Check the log for details!" ; fi
   #
   report "Extraction finished, done"
   #
   exit 0 
   #
elif [ $command = "put" ] ; then
   #
   #   and the put
   #
   filesize=$(stat "${filename}" -c%s)
   #
   #  check for existence of file
   #  NOTE : if the filesize is zero, we are expected to return 31, so that
   #         dcache can react accordingly.
   #
   [ -z "${filesize}" ] && problem 31 "File not found : ${filename}"
   if [ "${filesize}" = "0" ];
   then 
     result="dcache://dcache/store=${store}&group=${group}&bfid=${pnfsid}:*"
   else
     #
     #  now, finally copy the file to the HSM
     #  (we assume the bfid to be returned)
     #
     result=`datasetPut "${store}" "${group}" "${pnfsid}"` || exit $?
   fi
   #
   # osm://osm/?store=sql&group=chimera&bfid=3434.0.994.1188400818542
   #
   report "Result : ${result}"
   #
   echo "${result}"
   #
   exit 0
elif [ $command = "next" ] ; then
   echo "'next' operation not supported by this HSM" 1>&2
   exit 10 
else 
   echo "Illegal command $command" 1>&2
   exit 4
fi
exit 0
