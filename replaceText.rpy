init python:
    import os as zenpy_os
    import json as zenpy_json
    import string as zenpy_string
    import copy as zenpy_copy
    import io as zenpy_io

    if not hasattr(renpy,"session"):
        setattr(renpy, "session", {}) 

    renpy.session["zenpy_dir"] = __builtins__["dir"]
    renpy.session["zenpy_set"] = __builtins__["set"]
    renpy.session["zenpy_len"] = __builtins__["len"]

    if not "zenpy_variables" in renpy.session:
        renpy.session["zenpy_variables"] = {}

    if not "KeywordProcessorr" in renpy.session["zenpy_dir"]():
        class KeywordProcessor(object):
            """KeywordProcessor 

            Source Code: https://github.com/vi3k6i5/flashtext/

            Attributes:
                _keyword (str): Used as key to store keywords in trie dictionary.
                    Defaults to '_keyword_'
                non_word_boundaries (set(str)): Characters that will determine if the word is continuing.
                    Defaults to set([A-Za-z0-9_])
                keyword_trie_dict (dict): Trie dict built character by character, that is used for lookup
                    Defaults to empty dictionary
                case_sensitive (boolean): if the search algorithm should be case sensitive or not.
                    Defaults to False

            Examples:
                >>> # import module
                >>> from flashtext import KeywordProcessor
                >>> # Create an object of KeywordProcessor
                >>> keyword_processor = KeywordProcessor()
                >>> # add keywords
                >>> keyword_names = ['NY', 'new-york', 'SF']
                >>> clean_names = ['new york', 'new york', 'san francisco']
                >>> for keyword_name, clean_name in zip(keyword_names, clean_names):
                >>>     keyword_processor.add_keyword(keyword_name, clean_name)
                >>> keywords_found = keyword_processor.extract_keywords('I love SF and NY. new-york is the best.')
                >>> keywords_found
                >>> ['san francisco', 'new york', 'new york']

            Note:
                * loosely based on `Aho-Corasick algorithm <https://en.wikipedia.org/wiki/Aho%E2%80%93Corasick_algorithm>`_.
                * Idea came from this `Stack Overflow Question <https://stackoverflow.com/questions/44178449/regex-replace-is-taking-time-for-millions-of-documents-how-to-make-it-faster>`_.
            """

            def __init__(self, case_sensitive=False):
                """
                Args:
                    case_sensitive (boolean): Keyword search should be case sensitive set or not.
                        Defaults to False
                """
                self._keyword = '_keyword_'
                self._white_space_chars = renpy.session["zenpy_set"](['.', '\t', '\n', '\a', ' ', ','])
                try:
                    # python 2.x
                    self.non_word_boundaries = renpy.session["zenpy_set"](zenpy_string.digits + zenpy_string.letters + '_')
                except AttributeError:
                    # python 3.x
                    self.non_word_boundaries = renpy.session["zenpy_set"](zenpy_string.digits + zenpy_string.ascii_letters + '_')
                self.keyword_trie_dict = __builtins__["dict"]()
                self.case_sensitive = case_sensitive
                self._terms_in_trie = 0

            def __len__(self):
                """Number of terms present in the keyword_trie_dict

                Returns:
                    length : int
                        Count of number of distinct terms in trie dictionary.

                """
                return self._terms_in_trie

            def __contains__(self, word):
                """To check if word is present in the keyword_trie_dict

                Args:
                    word : string
                        word that you want to check

                Returns:
                    status : bool
                        If word is present as it is in keyword_trie_dict then we return True, else False

                Examples:
                    >>> keyword_processor.add_keyword('Big Apple')
                    >>> 'Big Apple' in keyword_processor
                    >>> # True

                """
                if not self.case_sensitive:
                    word = word.lower()
                current_dict = self.keyword_trie_dict
                len_covered = 0
                for char in word:
                    if char in current_dict:
                        current_dict = current_dict[char]
                        len_covered += 1
                    else:
                        break
                return self._keyword in current_dict and len_covered == renpy.session["zenpy_len"](word)

            def __getitem__(self, word):
                """if word is present in keyword_trie_dict return the clean name for it.

                Args:
                    word : string
                        word that you want to check

                Returns:
                    keyword : string
                        If word is present as it is in keyword_trie_dict then we return keyword mapped to it.

                Examples:
                    >>> keyword_processor.add_keyword('Big Apple', 'New York')
                    >>> keyword_processor['Big Apple']
                    >>> # New York
                """
                if not self.case_sensitive:
                    word = word.lower()
                current_dict = self.keyword_trie_dict
                len_covered = 0
                for char in word:
                    if char in current_dict:
                        current_dict = current_dict[char]
                        len_covered += 1
                    else:
                        break
                if self._keyword in current_dict and len_covered == renpy.session["zenpy_len"](word):
                    return current_dict[self._keyword]

            def __setitem__(self, keyword, clean_name=None):
                """To add keyword to the dictionary
                pass the keyword and the clean name it maps to.

                Args:
                    keyword : string
                        keyword that you want to identify

                    clean_name : string
                        clean term for that keyword that you would want to get back in return or replace
                        if not provided, keyword will be used as the clean name also.

                Examples:
                    >>> keyword_processor['Big Apple'] = 'New York'
                """
                status = False
                if not clean_name and keyword:
                    clean_name = keyword

                if keyword and clean_name:
                    if not self.case_sensitive:
                        keyword = keyword.lower()
                    current_dict = self.keyword_trie_dict
                    for letter in keyword:
                        current_dict = current_dict.setdefault(letter, {})
                    if self._keyword not in current_dict:
                        status = True
                        self._terms_in_trie += 1
                    current_dict[self._keyword] = clean_name
                return status

      

            def set_non_word_boundaries(self, non_word_boundaries):
                """set of characters that will be considered as part of word.

                Args:
                    non_word_boundaries (set(str)):
                        Set of characters that will be considered as part of word.

                """
                self.non_word_boundaries = non_word_boundaries

            def add_keyword(self, keyword, clean_name=None):
                """To add one or more keywords to the dictionary
                pass the keyword and the clean name it maps to.

                Args:
                    keyword : string
                        keyword that you want to identify

                    clean_name : string
                        clean term for that keyword that you would want to get back in return or replace
                        if not provided, keyword will be used as the clean name also.

                Returns:
                    status : bool
                        The return value. True for success, False otherwise.

                Examples:
                    >>> keyword_processor.add_keyword('Big Apple', 'New York')
                    >>> # This case 'Big Apple' will return 'New York'
                    >>> # OR
                    >>> keyword_processor.add_keyword('Big Apple')
                    >>> # This case 'Big Apple' will return 'Big Apple'
                """
                return self.__setitem__(keyword, clean_name)
            def try_replace(self,sentence):
                try:
                    return self.replace_keywords(sentence)
                except:
                    return sentence;

            def replace_keywords(self, sentence):
                """Searches in the string for all keywords present in corpus.
                Keywords present are replaced by the clean name and a new string is returned.

                Args:
                    sentence (str): Line of text where we will replace keywords

                Returns:
                    new_sentence (str): Line of text with replaced keywords

                Examples:
                    >>> from flashtext import KeywordProcessor
                    >>> keyword_processor = KeywordProcessor()
                    >>> keyword_processor.add_keyword('Big Apple', 'New York')
                    >>> keyword_processor.add_keyword('Bay Area')
                    >>> new_sentence = keyword_processor.replace_keywords('I love Big Apple and bay area.')
                    >>> new_sentence
                    >>> 'I love New York and Bay Area.'

                """
                if not sentence:
                    # if sentence is empty or none just return the same.
                    return sentence
                new_sentence = []
                orig_sentence = sentence
                if not self.case_sensitive:
                    sentence = sentence.lower()
                current_word = ''
                current_dict = self.keyword_trie_dict
                current_white_space = ''
                sequence_end_pos = 0
                idx = 0
                sentence_len = renpy.session["zenpy_len"](sentence)
                while idx < sentence_len:
                    char = sentence[idx]
                    # when we reach whitespace
                    if char not in self.non_word_boundaries:
                        current_word += orig_sentence[idx]
                        current_white_space = char
                        # if end is present in current_dict
                        if self._keyword in current_dict or char in current_dict:
                            # update longest sequence found
                            sequence_found = None
                            longest_sequence_found = None
                            is_longer_seq_found = False
                            if self._keyword in current_dict:
                                sequence_found = current_dict[self._keyword]
                                longest_sequence_found = current_dict[self._keyword]
                                sequence_end_pos = idx

                            # re look for longest_sequence from this position
                            if char in current_dict:
                                current_dict_continued = current_dict[char]
                                current_word_continued = current_word
                                idy = idx + 1
                                while idy < sentence_len:
                                    inner_char = sentence[idy]
                                    check_start_with_alpha =  not sentence[idx].isalpha()

                                    if inner_char not in self.non_word_boundaries and self._keyword in current_dict_continued and  ((idx-1 < 0 or not sentence[idx-1].isalpha())  or not sentence[idx].isalpha()) and (not sentence[idy].isalpha() or not sentence[idy-1].isalpha()): 
                                    
                                        #current_word_continued += orig_sentence[idy]
                                        # update longest sequence found
                                        current_white_space = inner_char
                                        longest_sequence_found = current_dict_continued[self._keyword]
                                        sequence_end_pos = idy
                                        is_longer_seq_found = True
                                    if inner_char in current_dict_continued:
                                        current_word_continued += orig_sentence[idy]
                                        current_dict_continued = current_dict_continued[inner_char]
                                    else:
                                        break
                                    idy += 1
                                else:
                                    # end of sentence reached.
                                    if self._keyword in current_dict_continued and (renpy.session["zenpy_len"](current_word_continued) == renpy.session["zenpy_len"](sentence) or (idy - renpy.session["zenpy_len"](current_word_continued) -1 > -1 and (not sentence[idy - renpy.session["zenpy_len"](current_word_continued)-1].isalpha() or not sentence[idx].isalpha()))):
                                        # update longest sequence found
                                        current_white_space = ''
                                        longest_sequence_found = current_dict_continued[self._keyword]
                                        sequence_end_pos = idy
                                        is_longer_seq_found = True
                                    
                                if is_longer_seq_found:
                                    idx = sequence_end_pos
                                    current_word = current_word_continued
                            current_dict = self.keyword_trie_dict
                            if longest_sequence_found:
                            
                                if current_word.islower():

                                    longest_sequence_found =  longest_sequence_found.lower()
                                    
                                elif current_word.isupper():

                                    longest_sequence_found = longest_sequence_found.upper()

                                elif renpy.session["zenpy_len"](current_word) > 1 and renpy.session["zenpy_len"](longest_sequence_found) > 1:

                                    if current_word[0].islower() and not longest_sequence_found[0].islower():

                                        longest_sequence_found_splited = [c for c in longest_sequence_found]
                                        longest_sequence_found_splited[0] = longest_sequence_found_splited[0].lower()
                                        longest_sequence_found = "".join(longest_sequence_found_splited)

                                    elif current_word[0].isupper() and  not longest_sequence_found[0].isupper():

                                        longest_sequence_found_splited = [c for c in longest_sequence_found]
                                        longest_sequence_found_splited[0] = longest_sequence_found_splited[0].upper()
                                        longest_sequence_found = "".join(longest_sequence_found_splited)

                                new_sentence.append(longest_sequence_found + current_white_space)
                                current_word = ''
                                current_white_space = ''
                            else:
                                new_sentence.append(current_word)
                                current_word = ''
                                current_white_space = ''
                        else:
                            # we reset current_dict
                            current_dict = self.keyword_trie_dict
                            new_sentence.append(current_word)
                            current_word = ''
                            current_white_space = ''
                    elif char in current_dict:
                        # we can continue from this char
                        current_word += orig_sentence[idx]
                        current_dict = current_dict[char]
                    else:
                        current_word += orig_sentence[idx]
                        # we reset current_dict
                        current_dict = self.keyword_trie_dict
                        # skip to end of word
                        idy = idx + 1
                        while idy < sentence_len:
                            char = sentence[idy]
                            current_word += orig_sentence[idy]
                            if char not in self.non_word_boundaries:
                                break
                            idy += 1
                        idx = idy
                        new_sentence.append(current_word)
                        current_word = ''
                        current_white_space = ''
                    # if we are end of sentence and have a sequence discovered
                    if idx + 1 >= sentence_len:
                        if self._keyword in current_dict:
                            sequence_found = current_dict[self._keyword]
                            new_sentence.append(sequence_found)
                        else:
                            new_sentence.append(current_word)
                    idx += 1
                return "".join(new_sentence)

    def get_file_from_apk(filename):
        if not renpy.android:
            return None
        if filename.find("x-") == -1:
            filename = "x-" + "/x-".join(filename.split("/"))
        if renpy.session["zenpy_len"](renpy.loader.game_apks) > 0:
            apk_object = renpy.loader.game_apks[0]
            binary_data = apk_object.open(filename)
            if binary_data == None:
                return binary_data
            return binary_data.getvalue().decode("utf8")
        return None 
    
    def zenpy_get_strings_path():
        zenpy_file = "/tl/HZIlle/strings.json"
        if zenpy_os.path.isfile(zenpy_file):
            return zenpy_file
        zenpy_file = (renpy.config.gamedir + "/tl/HZIlle/strings.json").replace("\\","/")
        if zenpy_os.path.isfile(zenpy_file):
            return zenpy_file
        zenpy_file = zenpy_file.replace("/","\\")
        if zenpy_os.path.isfile(zenpy_file):
            return zenpy_file
        if hasattr(renpy.config,"searchpath") and renpy.config.searchpath != None:
            for d in renpy.config.searchpath:
                zenpy_file = (d + "/tl/HZIlle/strings.json").replace("\\","/")
                if zenpy_os.path.isfile(zenpy_file):
                    return zenpy_file
                zenpy_file = zenpy_file.replace("/","\\")
                if zenpy_os.path.isfile(zenpy_file):
                    return zenpy_file
        return ""
        



    renpy.session["zenpy_variables"]["HZIlle"] = {"next_replace": None,"keyword_processor": KeywordProcessor(case_sensitive=False)}
    renpy.session["zenpy_variables"]["HZIlle"]["keyword_processor"].set_non_word_boundaries(renpy.session["zenpy_set"]("ñÑáéíóúÉÓÚàâäãèêëïîìôöòõùûüÿçÂÀÃÈÊËÎÌÔÛÙÜŸÇÒÕ"))
    zenpy_strings = []
    zenpy_file = zenpy_get_strings_path()
    
    if renpy.android and not zenpy_os.path.isfile(zenpy_file):
        zenpy_data = get_file_from_apk("tl/HZIlle/strings.json")
        if zenpy_data != None:
            zenpy_strings = zenpy_json.loads(zenpy_data)
    elif zenpy_os.path.isfile(zenpy_file):
        zenpy_opened_file = zenpy_io.open(zenpy_file, 'r',encoding="UTF8")
        zenpy_strings = zenpy_json.loads(zenpy_opened_file.read())
        zenpy_opened_file.close()
    else:
        print("FILE NOT FOUND :" + zenpy_file)


    if zenpy_strings != None and renpy.session["zenpy_len"](zenpy_strings) > 0:
        for original, replace in zenpy_strings.items():
            renpy.session["zenpy_variables"]["HZIlle"]["keyword_processor"].add_keyword(original.strip(),replace.strip())

    if not "__next_replace__" in renpy.session["zenpy_variables"]:
        renpy.session["zenpy_variables"]["__next_replace__"] = config.replace_text

    if not renpy.config.custom_text_tags:
        renpy.config.custom_text_tags["z" + "enpy_enable_tags"] = None

    def zenpy_text(text):
        if _preferences.language in renpy.session["zenpy_variables"]:
            return renpy.session["zenpy_variables"][_preferences.language]["keyword_processor"].try_replace(text)
        elif renpy.session["zenpy_variables"]["__next_replace__"] != None:
            return  renpy.session["zenpy_variables"]["__next_replace__"](text)
        else:
            return text
    
    config.replace_text = zenpy_text