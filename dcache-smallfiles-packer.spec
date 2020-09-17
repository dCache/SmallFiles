#
# Specfile for the dCache SmallFiles service
#
Summary: Scripts that allow handling of small files with dCache
Name: dcache-smallfiles-packer
Version: 1.2.5
Release: 1
License: GPL
Group: Applications/Services
Source: https://github.com/dCache/SmallFiles/archive/master/master.zip
URL: https://github.com/dCache/SmallFiles.git
BuildRoot: %{_tmppath}/%{name}-root
BuildArch: noarch
Distribution: dCache
Prefix: %{_prefix}
Vendor: dCache.org
Packager: dCache <support@dcache.org>

%description
dcache-smallfiles is a collection of scripts running as a service
to transparently combine bunches of small files in dCache into
large container files that are more suitable to be written to tape
systems.
   
%prep
rm -rf $RPM_BUILD_ROOT/*
rm -rf $RPM_BUILD_DIR/*
wget -O $RPM_SOURCE_DIR/master.zip https://github.com/dCache/SmallFiles/archive/master/master.zip
unzip $RPM_SOURCE_DIR/master.zip
mv SmallFiles*/* .

%build

%install
install --directory ${RPM_BUILD_ROOT}/etc/dcache
install --mode 644 $RPM_BUILD_DIR/src/skel/etc/dcache/container.conf ${RPM_BUILD_ROOT}/etc/dcache/container.conf
install --directory ${RPM_BUILD_ROOT}/etc/init.d
install --mode 755 $RPM_BUILD_DIR/src/skel/etc/init.d/pack-system ${RPM_BUILD_ROOT}/etc/init.d/pack-system
install --directory ${RPM_BUILD_ROOT}/usr/local/bin
install --mode 755 $RPM_BUILD_DIR/src/skel/usr/local/bin/dcap.py ${RPM_BUILD_ROOT}/usr/local/bin/dcap.py
install --mode 755 $RPM_BUILD_DIR/src/skel/usr/local/bin/pack-files.py ${RPM_BUILD_ROOT}/usr/local/bin/pack-files.py
install --mode 755 $RPM_BUILD_DIR/src/skel/usr/local/bin/fillmetadata.py ${RPM_BUILD_ROOT}/usr/local/bin/fillmetadata.py
install --mode 755 $RPM_BUILD_DIR/src/skel/usr/local/bin/writebfids.py ${RPM_BUILD_ROOT}/usr/local/bin/writebfids.py

%files
/etc/dcache/container.conf
/etc/init.d/pack-system
/usr/local/bin/dcap.py
/usr/local/bin/pack-files.py
/usr/local/bin/fillmetadata.py
/usr/local/bin/writebfids.py

