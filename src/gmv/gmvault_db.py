'''
    Gmvault: a tool to backup and restore your gmail account.
    Copyright (C) <2011-2012>  <guillaume Aubert (guillaume dot aubert at gmail do com)>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

'''
import json
import gzip
import re
import os
import itertools
import fnmatch
import shutil

import blowfish
import log_utils

import collections_utils
import gmvault_utils
import imap_utils
import credential_utils



LOG = log_utils.LoggerFactory.get_logger('gmvault_db')
            
class GmailStorer(object): #pylint:disable=R0902
    '''
       Store emails on disk
    ''' 
    DATA_FNAME     = "%s/%s.eml"
    METADATA_FNAME = "%s/%s.meta"
    CHAT_GM_LABEL  = "gmvault-chats"
    
    ID_K         = 'gm_id'
    EMAIL_K      = 'email'
    THREAD_IDS_K = 'thread_ids'
    LABELS_K     = 'labels'
    INT_DATE_K   = 'internal_date'
    FLAGS_K      = 'flags'
    SUBJECT_K    = 'subject'
    MSGID_K      = 'msg_id'
    
    HFIELDS_PATTERN = "[M,m][E,e][S,s][S,s][a,A][G,g][E,e]-[I,i][D,d]:\s+<(?P<msgid>.*)>\s+[S,s][U,u][b,B][J,j][E,e][C,c][T,t]:\s+(?P<subject>.*)\s*"
    
    HF_MSGID_PATTERN = ".*\s*[M,m][E,e][S,s][S,s][a,A][G,g][E,e]-[I,i][D,d]:\s+<(?P<msgid>.*)>"
    HF_SUB_PATTERN = ".*\s*[S,s][U,u][b,B][J,j][E,e][C,c][T,t]:\s+(?P<subject>.*)\s*"
    
    HFIELDS_RE      = re.compile(HFIELDS_PATTERN)
    
    HF_MSGID_RE     = re.compile(HF_MSGID_PATTERN)
    HF_SUB_RE       = re.compile(HF_SUB_PATTERN)
    
    ENCRYPTED_PATTERN = "[\w+,\.]+crypt[\w,\.]*"
    ENCRYPTED_RE      = re.compile(ENCRYPTED_PATTERN)
    
    
    DB_AREA                    = 'db'
    QUARANTINE_AREA            = 'quarantine'
    CHATS_AREA                 = 'chats'
    SUB_CHAT_AREA              = 'chats/%s'
    INFO_AREA                  = '.info'  # contains metadata concerning the database
    ENCRYPTION_KEY_FILENAME    = '.storage_key.sec'
    OLD_EMAIL_OWNER            = '.email_account.info' #deprecated
    EMAIL_OWNER                = '.owner_account.info'
    GMVAULTDB_VERSION          = '.gmvault_db_version.info'   
    
    def __init__(self, a_storage_dir, encrypt_data = False):
        """
           Store on disks
           args:
              a_storage_dir: Storage directory
              a_use_encryption: Encryption key. If there then encrypt
        """
        self._top_dir = a_storage_dir
        
        self._db_dir          = '%s/%s' % (a_storage_dir, GmailStorer.DB_AREA)
        self._quarantine_dir  = '%s/%s' % (a_storage_dir, GmailStorer.QUARANTINE_AREA)
        self._info_dir        = '%s/%s' % (a_storage_dir, GmailStorer.INFO_AREA)
        self._chats_dir       = '%s/%s' % (self._db_dir, GmailStorer.CHATS_AREA)
        
        self._sub_chats_dir   = None
        self._sub_chats_inc   = -1
        self._sub_chats_nb    = -1
        
        self._limit_per_chat_dir = gmvault_utils.get_conf_defaults().getint("General", "limit_per_chat_dir", 1500)
        
        #make dirs
        if not os.path.exists(self._db_dir):
            LOG.critical("No Storage DB in %s. Create it.\n" % (a_storage_dir))
        
        gmvault_utils.makedirs(self._db_dir)
        gmvault_utils.makedirs(self._chats_dir)
        gmvault_utils.makedirs(self._quarantine_dir)
        gmvault_utils.makedirs(self._info_dir)
        
        self.fsystem_info_cache = {}
        
        self._encrypt_data   = encrypt_data
        self._encryption_key = None
        self._cipher         = None
        
        #add version if it is needed to migrate gmvault-db in the future
        self._create_gmvault_db_version()
        
        
    def _init_sub_chats_dir(self):
        """
           get info from existing sub chats
        """
        nb_to_dir = {}
        
        LOG.debug("LIMIT_PER_CHAT_DIR = %s" % (self._limit_per_chat_dir) )
        
        if os.path.exists(self._chats_dir):
            dirs = os.listdir(self._chats_dir)
            for the_dir in dirs:
                the_split = the_dir.split("-")
                if len(the_split) != 2:
                    raise Exception("Should get 2 elements in %s" % (the_split))
                
                nb_to_dir[int(the_split[1])] = the_dir
                
            
            if len(nb_to_dir) == 0:
                # no sub dir yet. Set it up
                self._sub_chats_nb  = 0
                self._sub_chats_inc = 1
                self._sub_chats_dir = self.SUB_CHAT_AREA % ("subchats-%s" % (self._sub_chats_inc))
                gmvault_utils.makedirs("%s/%s" % (self._db_dir, self._sub_chats_dir))
            
            # treat when more than limit chats in max dir 
            # treat when no dirs
            # add limit  as attribute limit_per_dir = 2000
            else:
                the_max = max(nb_to_dir)
                files = os.listdir("%s/%s" % (self._chats_dir, nb_to_dir[the_max]))
                self._sub_chats_nb  = len(files)/2
                self._sub_chats_inc = the_max
                self._sub_chats_dir = self.SUB_CHAT_AREA % nb_to_dir[the_max] 
            
        
    def get_sub_chats_dir(self):
        """
           Get sub_chats_dir
        """
        if self._sub_chats_inc == -1:
            self._init_sub_chats_dir()
         
        if self._sub_chats_nb >= self._limit_per_chat_dir:
            self._sub_chats_inc += 1
            
            self._sub_chats_nb  = 1 
            
            self._sub_chats_dir = self.SUB_CHAT_AREA % ("subchats-%s" % (self._sub_chats_inc))
            gmvault_utils.makedirs('%s/%s' % (self._db_dir, self._sub_chats_dir))
            
            return self._sub_chats_dir
        else:
            self._sub_chats_nb += 1
            return self._sub_chats_dir
    
        
    
    def _create_gmvault_db_version(self):
        """
           Create the Gmvault database version if it doesn't already exist
        """
        version_file = '%s/%s' % (self._info_dir, self.GMVAULTDB_VERSION)
        if not os.path.exists(version_file):
            the_fd = open(version_file, "w+")
            the_fd.write(gmvault_utils.GMVAULT_VERSION)
            the_fd.close()
    
    def store_db_owner(self, email_owner):
        """
           Store the email owner in .info dir. This is used to avoid synchronizing multiple email accounts in gmvault-db.
           Always wipe out completly the file
        """
        owners = self.get_db_owners()
        
        if email_owner not in owners:
            owners.append(email_owner)
            the_fd = open('%s/%s' % (self._info_dir, self.EMAIL_OWNER), "w+")
            json.dump(owners, the_fd, ensure_ascii = False)
            the_fd.flush()
            the_fd.close()
        
    
    def get_db_owners(self):
        """
           Get the email owner for the gmvault-db. Because except in particular cases, the db will be only linked to one meail.
        """
        fname = '%s/%s' % (self._info_dir, self.EMAIL_OWNER)
        if os.path.exists(fname):    
            the_fd = open(fname)
            list_of_owners = json.load(the_fd)
            the_fd.close()
            return list_of_owners
        
        return []
   
    def get_info_dir(self):
        """
           Return the info dir of gmvault-db
        """ 
        return self._info_dir
        
    def get_encryption_cipher(self):
        """
           Return the cipher to encrypt an decrypt.
           If the secret key doesn't exist, it will be generated.
        """
        if not self._cipher:
            if not self._encryption_key:
                self._encryption_key = credential_utils.CredentialHelper.get_secret_key('%s/%s' % (self._info_dir, self.ENCRYPTION_KEY_FILENAME))
            
            #create blowfish cipher if data needs to be encrypted
            self._cipher = blowfish.Blowfish(self._encryption_key)
        
        return self._cipher
        
    @classmethod
    def get_encryption_key_path(cls, a_root_dir):
        """
           Return the path of the encryption key.
           This is used to print that information to the user
        """
        return  '%s/%s/%s' % (a_root_dir, cls.INFO_AREA, cls.ENCRYPTION_KEY_FILENAME)
    
    @classmethod
    def get_encryption_key(cls, a_info_dir):
        """
           Return or generate the encryption key if it doesn't exist
        """
        return credential_utils.CredentialHelper.get_secret_key('%s/%s' % (a_info_dir, cls.ENCRYPTION_KEY_FILENAME))
    
    @classmethod
    def parse_header_fields(cls, header_fields):
        """
           extract subject and message ids from the given header fields 
        """
        subject = None
        msgid   = None
        
        # look for subject
        matched = GmailStorer.HF_SUB_RE.match(header_fields)
        if matched:
            subject = matched.group('subject')
        
        # look for a msg id
        matched = GmailStorer.HF_MSGID_RE.match(header_fields)
        if matched:
            msgid = matched.group('msgid')
        
        return (subject, msgid)
    
    def get_all_chats_gmail_ids(self):
        """
           Get only chats dirs 
        """
        # first create a normal dir and sort it below with an OrderedDict
        # beware orderedDict preserve order by insertion and not by key order
        gmail_ids = {}
        
        chat_dir = '%s/%s' % (self._db_dir, self.CHATS_AREA)
        if os.path.exists(chat_dir):
            the_iter = gmvault_utils.ordered_dirwalk(chat_dir, "*.meta")
        
            #get all ids
            for filepath in the_iter:
                directory, fname = os.path.split(filepath)
                gmail_ids[long(os.path.splitext(fname)[0])] = os.path.basename(directory)

            #sort by key 
            #used own orderedDict to be compliant with version 2.5
            gmail_ids = collections_utils.OrderedDict(sorted(gmail_ids.items(), key=lambda t: t[0]))
        
        return gmail_ids
        
        
    def get_all_existing_gmail_ids(self, pivot_dir = None, ignore_sub_dir = ['chats']): #pylint:disable=W0102
        """
           get all existing gmail_ids from the database within the passed month 
           and all posterior months
        """
        # first create a normal dir and sort it below with an OrderedDict
        # beware orderedDict preserve order by insertion and not by key order
        gmail_ids = {}
        
        if pivot_dir == None:
            #the_iter = gmvault_utils.dirwalk(self._db_dir, "*.meta")
            the_iter = gmvault_utils.ordered_dirwalk(self._db_dir, "*.meta", ignore_sub_dir)
        else:
            
            # get all yy-mm dirs to list
            dirs = gmvault_utils.get_all_directories_posterior_to(pivot_dir, gmvault_utils.get_all_dirs_under(self._db_dir, ignore_sub_dir))
            
            #create all iterators and chain them to keep the same interface
            #iter_dirs = [gmvault_utils.dirwalk('%s/%s' % (self._db_dir, the_dir), "*.meta") for the_dir in dirs]
            iter_dirs = [gmvault_utils.ordered_dirwalk('%s/%s' % (self._db_dir, the_dir), "*.meta", ignore_sub_dir) for the_dir in dirs]
            
            the_iter = itertools.chain.from_iterable(iter_dirs)
        
        #get all ids
        for filepath in the_iter:
            directory, fname = os.path.split(filepath)
            gmail_ids[long(os.path.splitext(fname)[0])] = os.path.basename(directory)

        #sort by key 
        #used own orderedDict to be compliant with version 2.5
        gmail_ids = collections_utils.OrderedDict(sorted(gmail_ids.items(), key=lambda t: t[0]))
        
        return gmail_ids
    
    def bury_chat_metadata(self, email_info, local_dir = None):
        """
           Like bury metadata but with an extra label gmvault-chat
        """
        extra_labels = [GmailStorer.CHAT_GM_LABEL]
        return self.bury_metadata(email_info, local_dir, extra_labels)
    
    def bury_metadata(self, email_info, local_dir = None, extra_labels = []): #pylint:disable=W0102
        """
            Store metadata info in .meta file
            Arguments:
             email_info: metadata info
             local_dir : intermdiary dir (month dir)
        """
        if local_dir:
            the_dir = '%s/%s' % (self._db_dir, local_dir)
            gmvault_utils.makedirs(the_dir)
        else:
            the_dir = self._db_dir
         
        meta_path = self.METADATA_FNAME % (the_dir, email_info[imap_utils.GIMAPFetcher.GMAIL_ID])
       
        meta_desc = open(meta_path, 'w')
        
        # parse header fields to extract subject and msgid
        subject, msgid = self.parse_header_fields(email_info[imap_utils.GIMAPFetcher.IMAP_HEADER_FIELDS_KEY])
        
        # need to convert labels that are number as string
        # come from imap_lib when label is a number
        #labels = [ str(elem) for elem in  email_info[imap_utils.GIMAPFetcher.GMAIL_LABELS] ]
        labels = email_info[imap_utils.GIMAPFetcher.GMAIL_LABELS]
        labels.extend(extra_labels) #add extra labels
        
        #create json structure for metadata
        meta_obj = { 
                     self.ID_K         : email_info[imap_utils.GIMAPFetcher.GMAIL_ID],
                     self.LABELS_K     : labels,
                     self.FLAGS_K      : email_info[imap_utils.GIMAPFetcher.IMAP_FLAGS],
                     self.THREAD_IDS_K : email_info[imap_utils.GIMAPFetcher.GMAIL_THREAD_ID],
                     self.INT_DATE_K   : gmvault_utils.datetime2e(email_info[imap_utils.GIMAPFetcher.IMAP_INTERNALDATE]),
                     self.FLAGS_K      : email_info[imap_utils.GIMAPFetcher.IMAP_FLAGS],
                     self.SUBJECT_K    : subject,
                     self.MSGID_K      : msgid
                   }
        
<<<<<<< HEAD
        #json.dump(meta_obj, meta_desc, ensure_ascii = False)
=======
>>>>>>> 9596d158d3e205f8f1e3e7ff4f2786ac0728877c
        json.dump(meta_obj, meta_desc)
        
        meta_desc.flush()
        meta_desc.close()
         
        return email_info[imap_utils.GIMAPFetcher.GMAIL_ID]
    
    def bury_chat(self, chat_info, local_dir = None, compress = False):   
        """
            Like bury email but with a special label: gmvault-chats
            Arguments:
            chat_info: the chat content
            local_dir: intermediary dir
            compress : if compress is True, use gzip compression
        """
        extra_labels = ['gmvault-chats']
        
        return self.bury_email(chat_info, local_dir, compress, extra_labels)
        
    def bury_email(self, email_info, local_dir = None, compress = False, extra_labels = []): #pylint:disable=W0102
        """
           store all email info in 2 files (.meta and .eml files)
           Arguments:
             email_info: the email content
             local_dir : intermdiary dir (month dir)
             compress  : if compress is True, use gzip compression
        """
        
        if local_dir:
            the_dir = '%s/%s' % (self._db_dir, local_dir)
            gmvault_utils.makedirs(the_dir)
        else:
            the_dir = self._db_dir
        
        data_path = self.DATA_FNAME % (the_dir, email_info[imap_utils.GIMAPFetcher.GMAIL_ID])
        
        # if the data has to be encrypted
        if self._encrypt_data:
            data_path = '%s.crypt' % (data_path)
        
        if compress:
            data_path = '%s.gz' % (data_path)
            data_desc = gzip.open(data_path, 'wb')
        else:
            data_desc = open(data_path, 'wb')
            
        if self._encrypt_data:
            # need to be done for every encryption
            cipher = self.get_encryption_cipher()
            cipher.initCTR()
            data_desc.write(cipher.encryptCTR(email_info[imap_utils.GIMAPFetcher.EMAIL_BODY]))
        else:
            data_desc.write(email_info[imap_utils.GIMAPFetcher.EMAIL_BODY])
            
        # parse header fields to extract subject and msgid
        subject, msgid = self.parse_header_fields(email_info[imap_utils.GIMAPFetcher.IMAP_HEADER_FIELDS_KEY])
        
        # need to convert labels that are number as string
        # come from imap_lib when label is a number
        #labels = [ str(elem) for elem in  email_info[imap_utils.GIMAPFetcher.GMAIL_LABELS] ]
        labels = []
        for label in  email_info[imap_utils.GIMAPFetcher.GMAIL_LABELS]:
            if isinstance(label, (int, long, float, complex)):
                label = str(label)

            labels.append(unicode(label))
        
        labels.extend(extra_labels) #add extra labels
        
        #create json structure for metadata
        meta_obj = { 
                     self.ID_K         : email_info[imap_utils.GIMAPFetcher.GMAIL_ID],
                     self.LABELS_K     : labels,
                     self.FLAGS_K      : email_info[imap_utils.GIMAPFetcher.IMAP_FLAGS],
                     self.THREAD_IDS_K : email_info[imap_utils.GIMAPFetcher.GMAIL_THREAD_ID],
                     self.INT_DATE_K   : gmvault_utils.datetime2e(email_info[imap_utils.GIMAPFetcher.IMAP_INTERNALDATE]),
                     self.SUBJECT_K    : subject,
                     self.MSGID_K      : msgid
                   }
        
        meta_desc = open(self.METADATA_FNAME % (the_dir, email_info[imap_utils.GIMAPFetcher.GMAIL_ID]), 'w')
        
        json.dump(meta_obj, meta_desc)
        
        meta_desc.flush()
        meta_desc.close()
        
        data_desc.flush()
        data_desc.close()
        
        return email_info[imap_utils.GIMAPFetcher.GMAIL_ID]
    
    def get_directory_from_id(self, a_id, a_local_dir = None):
        """
           If a_local_dir (yy_mm dir) is passed, check that metadata file exists and return dir
           Return the directory path if id located.
           Return None if not found
        """
        filename = '%s.meta' % (a_id)
        
        #local_dir can be passed to avoid scanning the filesystem (because of WIN7 fs weaknesses)
        if a_local_dir:
            the_dir = '%s/%s' % (self._db_dir, a_local_dir)
            if os.path.exists(self.METADATA_FNAME % (the_dir, a_id)):
                return the_dir
            else:
                return None
        
        # first look in cache
        for the_dir in self.fsystem_info_cache:
            if filename in self.fsystem_info_cache[the_dir]:
                return the_dir
        
        #walk the filesystem
        for the_dir, _, files in os.walk(os.path.abspath(self._db_dir)):
            self.fsystem_info_cache[the_dir] = files
            for filename in fnmatch.filter(files, filename):
                return the_dir
        
        return None
    
    def _get_data_file_from_id(self, a_dir, a_id):
        """
           Return data file from the id
        """
        data_p = self.DATA_FNAME % (a_dir, a_id)
        
        # check if encrypted and compressed or not
        if os.path.exists('%s.crypt.gz' % (data_p)):
            data_fd = gzip.open('%s.crypt.gz' % (data_p), 'r')
        elif os.path.exists('%s.gz' % (data_p)):
            data_fd = gzip.open('%s.gz' % (data_p), 'r')
        elif os.path.exists('%s.crypt' % (data_p)):
            data_fd = open('%s.crypt' % (data_p), 'r')
        else:
            data_fd = open(data_p)
        
        return data_fd
    
    def _get_metadata_file_from_id(self, a_dir, a_id):
        """
           metadata file
        """
        meta_p = self.METADATA_FNAME % (a_dir, a_id)
       
        return open(meta_p)
    
    def quarantine_email(self, a_id):
        """
           Quarantine the email
        """
        #get the dir where the email is stored
        the_dir = self.get_directory_from_id(a_id)
        
        data = self.DATA_FNAME % (the_dir, a_id)
        
        # check if encrypted and compressed or not
        if os.path.exists('%s.crypt.gz' % (data)):
            data = '%s.crypt.gz' % (data)
        elif os.path.exists('%s.gz' % (data)):
            data = '%s.gz' % (data)
        elif os.path.exists('%s.crypt' % (data)):
            data = '%s.crypt' % (data)
        
        meta = self.METADATA_FNAME % (the_dir, a_id)

        #remove files if already quarantined
        q_data_path = os.path.join(self._quarantine_dir, os.path.basename(data))
        q_meta_path = os.path.join(self._quarantine_dir, os.path.basename(meta))

        if os.path.exists(q_data_path):
            os.remove(q_data_path)        
        
        if os.path.exists(q_meta_path):
            os.remove(q_meta_path)

        shutil.move(data, self._quarantine_dir)
        shutil.move(meta, self._quarantine_dir)
        
    def email_encrypted(self, a_email_fn):
        """
           True is filename contains .crypt otherwise False
        """
        basename = os.path.basename(a_email_fn)
        if self.ENCRYPTED_RE.match(basename):
            return True
        else:
            return False
        
    def unbury_email(self, a_id):
        """
           Restore the complete email info from info stored on disk
           Return a tuple (meta, data)
        """
        the_dir = self.get_directory_from_id(a_id)

        #LOG.debug("unbury_email: Dir = %s, id = %s" % (the_dir, a_id))
        
        data_fd = self._get_data_file_from_id(the_dir, a_id)
        
        if self.email_encrypted(data_fd.name):
            LOG.debug("Restore encrypted email %s" % (a_id))
            # need to be done for every encryption
            cipher = self.get_encryption_cipher()
            cipher.initCTR()
            data = cipher.decryptCTR(data_fd.read())
        else:
            data = data_fd.read()
        
        return (self.unbury_metadata(a_id, the_dir), data)
    
    def unbury_data(self, a_id, a_id_dir = None):
        """
           Get the only the email content from the DB
        """
        if not a_id_dir:
            a_id_dir = self.get_directory_from_id(a_id)
            
        data_fd = self._get_data_file_from_id(a_id_dir, a_id)
        
        if self.email_encrypted(data_fd.name):
            LOG.debug("Restore encrypted email %s" % (a_id))
            # need to be done for every encryption
            cipher = self.get_encryption_cipher()
            cipher.initCTR()
            data = cipher.decryptCTR(data_fd.read())
        else:
            data = data_fd.read()
            
        return data    
        
    def unbury_metadata(self, a_id, a_id_dir = None):
        """
           Get metadata info from DB
        """
        if not a_id_dir:
            a_id_dir = self.get_directory_from_id(a_id)
        
        meta_fd = self._get_metadata_file_from_id(a_id_dir, a_id)
    
        #metadata = json.load(meta_fd, encoding="utf-8")
        metadata = json.load(meta_fd)
        
        metadata[self.INT_DATE_K] =  gmvault_utils.e2datetime(metadata[self.INT_DATE_K])
        
        # force convertion of labels as string because IMAPClient
        # returns a num when the label is a number (ie. '00000') and handle utf-8
        new_labels = []

        for label in  metadata[self.LABELS_K]:
            if isinstance(label, (int, long, float, complex)):
                label = str(label)

<<<<<<< HEAD
            #new_labels.append(label.encode('utf-8'))
=======
>>>>>>> 9596d158d3e205f8f1e3e7ff4f2786ac0728877c
            new_labels.append(unicode(label))
 
        metadata[self.LABELS_K] = new_labels

        #force encoding in utf-8
        #metadata[self.LABELS_K] = [ str(elem).encode('utf-8') \
		#                            if isinstance(label, (int, long, float, complex)) \
		#							 else elem.encode('utf-8') \
	    #							 for elem in  metadata[self.LABELS_K] ]
        
        return metadata
    
    def delete_emails(self, emails_info, msg_type):
        """
           Delete all emails and metadata with ids
        """
        if msg_type == 'email':
            db_dir = self._db_dir
        else:
            db_dir = self._chats_dir
        
        for (a_id, date_dir) in emails_info:
            
            the_dir = '%s/%s' % (db_dir, date_dir)
            
            data_p      = self.DATA_FNAME % (the_dir, a_id)
            comp_data_p = '%s.gz' % (data_p)
            cryp_comp_data_p = '%s.crypt.gz' % (data_p)
            
            metadata_p  = self.METADATA_FNAME % (the_dir, a_id)
            
            #delete files if they exists
            if os.path.exists(data_p):
                os.remove(data_p)
            elif os.path.exists(comp_data_p):
                os.remove(comp_data_p)
            elif os.path.exists(cryp_comp_data_p):
                os.remove(cryp_comp_data_p)   
            
            if os.path.exists(metadata_p):
                os.remove(metadata_p)
